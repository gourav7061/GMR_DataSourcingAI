"""Extract income statement, balance sheet and cash flow from downloaded reports using Claude.

Usage:
    py tools/extract_financials.py <documents.json> [--force] [--workers 4]

documents.json is produced by download_reports.py. Each extraction is cached in
.tmp/extractions/ keyed by file hash, so re-runs cost nothing unless --force.
"""
import argparse
import base64
import hashlib
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import EXTRACT_DIR, call_json, get_client

PROMPT_VERSION = 1
EFFORT = "medium"
MAX_FULL_PDF_PAGES = 120

TITLE_RE = re.compile(
    r"(statements? of (consolidated )?(profit or loss|comprehensive income|income|operations|earnings|"
    r"financial position|cash flows?)|balance sheets?|income statements?|cash flows? statements?|"
    r"profit and loss)",
    re.I,
)

ROW = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "level": {"type": "integer"},
        "is_total": {"type": "boolean"},
        "unscaled": {"type": "boolean"},
        "values": {"type": "array", "items": {"type": ["number", "null"]}},
    },
    "required": ["label", "level", "is_total", "unscaled", "values"],
    "additionalProperties": False,
}
COLUMN = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "period_end": {"type": "string"},
        "duration_months": {"type": "integer"},
    },
    "required": ["label", "period_end", "duration_months"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "currency": {"type": "string"},
        "statements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string", "enum": ["income_statement", "balance_sheet", "cash_flow"]},
                    "title": {"type": "string"},
                    "unit_label": {"type": "string"},
                    "unit_multiplier": {"type": "integer", "enum": [1, 1000, 1000000, 1000000000]},
                    "columns": {"type": "array", "items": COLUMN},
                    "rows": {"type": "array", "items": ROW},
                },
                "required": ["statement", "title", "unit_label", "unit_multiplier", "columns", "rows"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["company", "currency", "statements"],
    "additionalProperties": False,
}

SYSTEM = """You transcribe a company's primary financial statements from a financial report into JSON, exactly as published.

Extract three statements:
- income_statement: the statement of profit or loss / income statement / statement of operations. If comprehensive income is a separate statement, skip it; if it is combined with profit or loss, include every row.
- balance_sheet: the balance sheet / statement of financial position.
- cash_flow: the statement of cash flows.
Use the primary statements from the financial-statements section, not summary tables in the management discussion. If a statement continues across pages, return it once with all rows. Omit a statement only if the document does not contain it. Ignore notes.

Columns: include every period column the statement shows (e.g. three months and six months, current and prior year). For each column give:
- label in the company's fiscal convention: "Q2 2025" for a 3-month quarter, "H1 2025" for six months, "9M 2025" for nine months, "FY2025" for a full fiscal year, and "As of 2025-06-30" for balance-sheet dates. Use the fiscal year the company uses.
- period_end as YYYY-MM-DD and duration_months (3, 6, 9, 12; 0 for balance-sheet dates).

Rows: every line item in published order, including headings and subtotals, with the label text as printed (English). level is the indentation depth (0 = section heading or top level, 1 = item under it, and so on). is_total marks subtotals and totals. Headings without numbers get null values. values has one entry per column, in column order.

Numbers: transcribe as plain numbers in the statement's own unit, without rescaling. Parentheses or a leading minus mean negative. A dash or blank means null. unit_label is the unit as stated (e.g. "RMB million"); unit_multiplier is its scale (1, 1000, 1000000 or 1000000000). Set unscaled=true on rows whose values are not in that unit: per-share amounts, share counts, percentages.
Accuracy matters more than anything else: copy each figure exactly."""


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:20]


def select_pdf_pages(path: Path) -> tuple[bytes, int, int]:
    """Return a smaller PDF holding only statement pages (plus the page after each). Returns (pdf_bytes, kept, total)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(path))
    total = len(reader.pages)
    keep = set()
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        head = "\n".join(text.strip().splitlines()[:6])
        if TITLE_RE.search(head):
            keep.update({i, i + 1})
    keep = sorted(k for k in keep if k < total)
    if not keep:
        if total > MAX_FULL_PDF_PAGES:
            raise RuntimeError(
                f"Could not locate statement pages in a {total}-page PDF (it may be scanned). "
                f"Sending it whole would be expensive, so it was skipped."
            )
        return path.read_bytes(), total, total
    writer = PdfWriter()
    for k in keep:
        writer.add_page(reader.pages[k])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), len(keep), total


def _html_text(path: Path) -> str:
    from bs4 import BeautifulSoup

    text = BeautifulSoup(path.read_bytes(), "html.parser").get_text("\n")
    text = re.sub(r"\n\s*\n+", "\n", text)
    if len(text) <= 400_000:
        return text
    parts = [text[m.start(): m.start() + 30_000] for m in TITLE_RE.finditer(text)]
    return "\n...\n".join(parts) or text[:400_000]


def _sheet_text(path: Path) -> str:
    if path.suffix.lower() == ".csv":
        return path.read_text(encoding="utf-8", errors="replace")
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"### Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                out.append(",".join("" if v is None else str(v) for v in row))
    return "\n".join(out)


def build_content(path: Path, log=print) -> list:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pdf, kept, total = select_pdf_pages(path)
        log(f"  {path.name}: sending {kept} of {total} pages")
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                 "data": base64.standard_b64encode(pdf).decode()}}
    elif suffix in (".htm", ".html"):
        block = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": _html_text(path)}}
    elif suffix in (".xlsx", ".xlsm", ".csv"):
        block = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": _sheet_text(path)}}
    else:
        raise RuntimeError(f"Unsupported file type: {suffix}")
    return [block, {"type": "text", "text": "Extract the income statement, balance sheet and cash flow statement."}]


def latest_period(data: dict) -> tuple[str, str]:
    """(period_end, label) of the latest column; at the same date the longest duration wins (FY over Q4)."""
    cols = [c for st in data["statements"] for c in st["columns"] if re.match(r"\d{4}-\d{2}-\d{2}", c["period_end"])]
    if not cols:
        return "", ""
    best = max(cols, key=lambda c: (c["period_end"], c["duration_months"]))
    return best["period_end"], best["label"]


def _with_source(data: dict, doc: dict) -> dict:
    path = Path(doc["file"])
    source = {k: doc.get(k) for k in ("period_label", "period_end", "doc_type", "href", "file")}
    source["file_name"] = doc.get("file_name") or path.name
    if not source["period_end"]:
        source["period_end"], label = latest_period(data)
        source["period_label"] = label or source["file_name"]
    return {**data, "source": source}


def extract_one(doc: dict, client=None, force: bool = False, log=print) -> dict:
    path = Path(doc["file"])
    name = doc.get("file_name") or doc["period_label"]
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    cache = EXTRACT_DIR / f"{file_hash(path)}_v{PROMPT_VERSION}.json"
    if cache.exists() and not force:
        log(f"Cached extraction: {name}")
        data = json.loads(cache.read_text(encoding="utf-8"))
        return {**_with_source(data, doc), "cost_usd": 0.0, "cached": True}
    if path.suffix.lower() == ".xls":
        raise RuntimeError("Old .xls files aren't supported. Open it in Excel and save as .xlsx.")
    client = client or get_client()
    log(f"Extracting {name}...")
    data, cost = call_json(client, build_content(path, log), SCHEMA, effort=EFFORT, system=SYSTEM)
    for st in data["statements"]:
        n = len(st["columns"])
        for row in st["rows"]:
            row["values"] = (row["values"] + [None] * n)[:n]
    cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    data = _with_source(data, doc)
    found = ", ".join(s["statement"] for s in data["statements"]) or "none"
    log(f"Done {name} -> {data['source']['period_label']}: {found} (${cost:.2f})")
    return {**data, "cost_usd": cost, "cached": False}


def extract_many(docs: list[dict], force: bool = False, workers: int = 4, log=print) -> tuple[list[dict], list[dict]]:
    client = get_client()
    docs = [d for d in docs if d.get("file")]

    def work(d):
        try:
            return extract_one(d, client, force, log)
        except Exception as e:
            log(f"FAILED {d['period_label']}: {e}")
            return {"error": str(e), "source": d}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, docs))
    ok = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    return ok, failed


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("documents_json")
    ap.add_argument("--force", action="store_true", help="re-extract even if cached")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    meta = json.loads(Path(args.documents_json).read_text(encoding="utf-8"))
    ok, failed = extract_many(meta["documents"], args.force, args.workers)
    out = Path(args.documents_json).with_name("extractions.json")
    out.write_text(json.dumps({"company": meta["company"], "extractions": ok, "failed": failed}, indent=1), encoding="utf-8")
    print(json.dumps({"extracted": len(ok), "failed": len(failed),
                      "cost_usd": round(sum(r["cost_usd"] for r in ok), 2), "output": str(out)}))
