"""Extract GAAP -> non-GAAP reconciliations (adjusted / non-IFRS measures) from reports using Claude.

This is a second pass that runs alongside extract_financials.py. It has its own page filter, prompt and
cache (<hash>_nongaap_v<N>.json), so the primary-statement extraction is unaffected.

Usage:
    py tools/extract_non_gaap.py <documents.json> [--force] [--workers 4]

Writes non_gaap.json next to documents.json. Files without any non-GAAP wording are skipped
without a Claude call (cost $0) and cached as empty.
"""
import argparse
import base64
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import EXTRACT_DIR, call_json, get_client
from extract_financials import _sheet_text, file_hash, latest_period

NON_GAAP_PROMPT_VERSION = 1
EFFORT = "medium"
MAX_PAGES = 30

KEYWORD_RE = re.compile(
    r"non[- ]?(gaap|ifrs|hkfrs|ind[- ]?as)|adjusted (net|ebitda|ebit|operating|earnings|profit|diluted|basic|eps)|"
    r"reconciliation of (gaap|ifrs|non)|underlying (profit|earnings)",
    re.I,
)

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
ROW = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "role": {"type": "string", "enum": ["gaap", "adjustment", "non_gaap", "other"]},
        "level": {"type": "integer"},
        "unscaled": {"type": "boolean"},
        "values": {"type": "array", "items": {"type": ["number", "null"]}},
    },
    "required": ["label", "role", "level", "unscaled", "values"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "currency": {"type": "string"},
        "gaap_basis": {"type": "string"},
        "reconciliations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "measure": {"type": "string"},
                    "title": {"type": "string"},
                    "unit_label": {"type": "string"},
                    "unit_multiplier": {"type": "integer", "enum": [1, 1000, 1000000, 1000000000]},
                    "columns": {"type": "array", "items": COLUMN},
                    "rows": {"type": "array", "items": ROW},
                },
                "required": ["measure", "title", "unit_label", "unit_multiplier", "columns", "rows"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["company", "currency", "gaap_basis", "reconciliations"],
    "additionalProperties": False,
}

SYSTEM = """You transcribe a company's reconciliations of non-GAAP measures (also called non-IFRS, adjusted, underlying or alternative performance measures) to their nearest GAAP/IFRS measures into JSON, exactly as published.

gaap_basis: the accounting standard the company reports under, as it names it (e.g. "US GAAP", "IFRS", "HKFRS", "Ind AS").

Output one reconciliation per measure being reconciled (e.g. operating profit, profit for the period, net income attributable to shareholders, EBITDA, diluted EPS). Always normalize it to this shape, whatever the published layout:
- rows, top to bottom: the GAAP / as-reported line (role "gaap"), then each adjustment in published order (role "adjustment", level 1), then the resulting non-GAAP / adjusted line (role "non_gaap"). Use role "other" only for lines that fit none of these (e.g. a subtotal inside the adjustments).
- columns: one per period. Many companies print a separate table per period with measures as rows and adjustments as columns; transpose these and combine all periods of the same measure into one reconciliation.
- measure: the GAAP measure's name as printed (e.g. "Profit for the period"). Use the same name for the same measure across periods.
- Ratios and per-share figures tied to a measure (net margin, adjusted EPS) go in the same reconciliation as extra rows labelled with their basis, e.g. "Net margin (as reported)" role "gaap" and "Net margin (non-IFRS)" role "non_gaap", with unscaled=true. Give percentages as numbers (8.7% -> 8.7).
- Label each row as printed (English), adding the basis in brackets where the printed label alone is ambiguous (e.g. "Profit for the period (non-IFRS)").
If an EBITDA or adjusted EBITDA reconciliation starts from net profit, it is still one reconciliation with the net profit line as role "gaap".

Columns: label in the company's fiscal convention ("Q2 2025" for a 3-month quarter, "H1 2025", "9M 2025", "FY2025"), period_end as YYYY-MM-DD, duration_months (3, 6, 9 or 12).

Numbers: plain numbers in the table's own unit, no rescaling. Parentheses or a leading minus mean negative; keep each adjustment's sign as printed. A dash or blank means null. unit_label is the unit as stated (e.g. "RMB in thousand"); unit_multiplier its scale. Set unscaled=true on per-share amounts, share counts and percentages.

Use only reconciliation tables (and non-GAAP highlight tables that show both bases side by side if no reconciliation exists). Ignore the primary financial statements and narrative text. If the document has no non-GAAP measures, return an empty reconciliations list.
Accuracy matters more than anything else: copy each figure exactly."""


def select_non_gaap_pages(path: Path) -> tuple[bytes | None, int, int]:
    """PDF holding only pages that mention non-GAAP measures (plus the page after each). None if there are none."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(path))
    total = len(reader.pages)
    hits = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if KEYWORD_RE.search(text):
            hits.append(i)
    if not hits:
        return None, 0, total
    # prefer pages that actually look like reconciliation tables when there are many mentions
    keep = sorted({k for i in hits for k in (i, i + 1) if k < total})
    if len(keep) > MAX_PAGES:
        recon = [i for i in hits if re.search(r"reconcil", reader.pages[i].extract_text() or "", re.I)]
        keep = sorted({k for i in (recon or hits) for k in (i, i + 1) if k < total})[:MAX_PAGES]
    writer = PdfWriter()
    for k in keep:
        writer.add_page(reader.pages[k])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), len(keep), total


def _html_text(path: Path) -> str | None:
    from bs4 import BeautifulSoup

    text = BeautifulSoup(path.read_bytes(), "html.parser").get_text("\n")
    text = re.sub(r"\n\s*\n+", "\n", text)
    matches = list(KEYWORD_RE.finditer(text))
    if not matches:
        return None
    if len(text) <= 400_000:
        return text
    spans, parts = [], []
    for m in matches:
        s, e = max(0, m.start() - 3_000), m.start() + 15_000
        if spans and s <= spans[-1][1]:
            spans[-1][1] = e
        else:
            spans.append([s, e])
    for s, e in spans:
        parts.append(text[s:e])
    return "\n...\n".join(parts)[:400_000]


def build_content(path: Path, log=print) -> list | None:
    """Claude content blocks, or None when the file has no non-GAAP wording (no call needed)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pdf, kept, total = select_non_gaap_pages(path)
        if pdf is None:
            return None
        log(f"  {path.name}: non-GAAP pass sending {kept} of {total} pages")
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                 "data": base64.standard_b64encode(pdf).decode()}}
    elif suffix in (".htm", ".html"):
        text = _html_text(path)
        if text is None:
            return None
        block = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text}}
    elif suffix in (".xlsx", ".xlsm", ".csv"):
        text = _sheet_text(path)
        if not KEYWORD_RE.search(text):
            return None
        block = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text}}
    else:
        raise RuntimeError(f"Unsupported file type: {suffix}")
    return [block, {"type": "text", "text": "Extract every GAAP to non-GAAP reconciliation."}]


def _empty() -> dict:
    return {"company": "", "currency": "", "gaap_basis": "", "reconciliations": []}


def _with_source(data: dict, doc: dict) -> dict:
    path = Path(doc["file"])
    source = {k: doc.get(k) for k in ("period_label", "period_end", "doc_type", "href", "file")}
    source["file_name"] = doc.get("file_name") or path.name
    if not source["period_end"]:
        source["period_end"], label = latest_period({"statements": data["reconciliations"]})
        source["period_label"] = label or source["file_name"]
    return {**data, "source": source}


def extract_non_gaap_one(doc: dict, client=None, force: bool = False, log=print) -> dict:
    path = Path(doc["file"])
    name = doc.get("file_name") or doc["period_label"]
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    cache = EXTRACT_DIR / f"{file_hash(path)}_nongaap_v{NON_GAAP_PROMPT_VERSION}.json"
    if cache.exists() and not force:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return {**_with_source(data, doc), "cost_usd": 0.0, "cached": True}
    if path.suffix.lower() == ".xls":
        raise RuntimeError("Old .xls files aren't supported. Open it in Excel and save as .xlsx.")
    content = build_content(path, log)
    if content is None:
        data, cost = _empty(), 0.0
        log(f"No non-GAAP measures mentioned in {name}")
    else:
        client = client or get_client()
        log(f"Extracting non-GAAP measures from {name}...")
        data, cost = call_json(client, content, SCHEMA, effort=EFFORT, system=SYSTEM)
        for rec in data["reconciliations"]:
            n = len(rec["columns"])
            for row in rec["rows"]:
                row["values"] = (row["values"] + [None] * n)[:n]
        log(f"Done non-GAAP {name}: {len(data['reconciliations'])} reconciliations (${cost:.2f})")
    cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return {**_with_source(data, doc), "cost_usd": cost, "cached": False}


def extract_many(docs: list[dict], force: bool = False, workers: int = 4, log=print) -> tuple[list[dict], list[dict]]:
    client = get_client()
    docs = [d for d in docs if d.get("file")]

    def work(d):
        try:
            return extract_non_gaap_one(d, client, force, log)
        except Exception as e:
            log(f"FAILED non-GAAP {d['period_label']}: {e}")
            return {"error": str(e), "source": d}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, docs))
    return [r for r in results if "error" not in r], [r for r in results if "error" in r]


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
    out = Path(args.documents_json).with_name("non_gaap.json")
    out.write_text(json.dumps({"company": meta["company"], "extractions": ok, "failed": failed}, indent=1),
                   encoding="utf-8")
    print(json.dumps({"extracted": len(ok), "failed": len(failed),
                      "cost_usd": round(sum(r["cost_usd"] for r in ok), 2), "output": str(out)}))
