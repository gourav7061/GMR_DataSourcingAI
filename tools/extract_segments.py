"""Extract every revenue breakdown (business segments, product lines, geography, other splits) with segment
gross profit / margin from reports using Claude.

This is a third pass that runs alongside extract_financials.py and extract_non_gaap.py. It has its own
page filter, prompt and cache (<hash>_segments_v<N>.json), so the other passes are unaffected.

Usage:
    py tools/extract_segments.py <documents.json> [--force] [--workers 4]

Writes segments.json next to documents.json. Files without segment tables are skipped without a
Claude call (cost $0) and cached as empty.
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

SEGMENTS_PROMPT_VERSION = 1
EFFORT = "medium"
MAX_PAGES = 30
MIN_TABLE_NUMBERS = 8  # a keyword page must also carry this many comma-grouped numbers to count as a table

KEYWORD_RE = re.compile(
    r"segment|revenues? by|by (geograph|region|product|channel|market)|disaggregat|breakdown of revenue|"
    r"geographical information",
    re.I,
)
NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+")

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
        "metric": {"type": "string", "enum": ["revenue", "gross_profit", "gross_margin"]},
        "parent": {"type": "string"},
        "level": {"type": "integer"},
        "is_total": {"type": "boolean"},
        "unscaled": {"type": "boolean"},
        "values": {"type": "array", "items": {"type": ["number", "null"]}},
    },
    "required": ["label", "metric", "parent", "level", "is_total", "unscaled", "values"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "currency": {"type": "string"},
        "breakdowns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string", "enum": ["segment", "geography", "other"]},
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                    "unit_label": {"type": "string"},
                    "unit_multiplier": {"type": "integer", "enum": [1, 1000, 1000000, 1000000000]},
                    "columns": {"type": "array", "items": COLUMN},
                    "rows": {"type": "array", "items": ROW},
                },
                "required": ["dimension", "name", "title", "unit_label", "unit_multiplier", "columns", "rows"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["company", "currency", "breakdowns"],
    "additionalProperties": False,
}

SYSTEM = """You transcribe every breakdown of a company's revenue from a financial report into JSON, exactly as published.

Breakdowns (one per dimension):
- dimension "segment": business / reportable segments and, nested under them, the product or sub-segment lines the report shows (e.g. segment "Smartphone x AIoT" with lines "Smartphones", "IoT and lifestyle products", "Internet services"). Segment rows have level 0 and parent ""; their lines have level 1 and parent = the segment label exactly as you wrote it. If the report has no segment grouping, list the lines at level 0. name: "By business segment".
- dimension "geography": revenue by region or country. name: "By geography".
- dimension "other": any other revenue split (by channel, customer type, timing of revenue recognition, contract type...). One breakdown each, name describing it, e.g. "By channel".
The same breakdown often appears more than once (management discussion tables, segment note, revenue note). Return it ONCE, merging all its tables into the most granular version, with all periods any of them show.

Rows in each breakdown, in this order:
1. metric "revenue": every line in published order, then the total line (is_total=true) if printed.
2. metric "gross_profit": the same lines, where gross profit is disclosed.
3. metric "gross_margin": the same lines, where gross margin is disclosed, as percentage numbers (20.0% -> 20.0), unscaled=true.
Use the same label for a line across metrics. Omit metrics the report does not disclose for that breakdown. Skip segment operating profit, cost of sales, "% of total revenue" and year-over-year change columns, non-financial KPIs (units shipped, users, ASP) and figures that appear only in narrative text.

Columns: one per period ("Q2 2025" for a 3-month quarter, "H1 2025", "9M 2025", "FY2025"), with period_end as YYYY-MM-DD and duration_months (3, 6, 9 or 12). Transpose tables that print periods as rows.

Numbers: plain numbers in the table's own unit, no rescaling. Parentheses or a leading minus mean negative. A dash or blank means null. unit_label is the unit as stated (e.g. "RMB in millions"); unit_multiplier its scale. If tables of one breakdown use different units, convert to the unit of the most granular table and say so in unit_label.

Ignore the primary financial statements. If the document has no revenue breakdown, return an empty breakdowns list.
Accuracy matters more than anything else: copy each figure exactly."""


def select_segment_pages(path: Path) -> tuple[bytes | None, int, int]:
    """PDF holding only pages with segment / revenue-breakdown tables (plus the page after each). None if none."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(path))
    total = len(reader.pages)
    hits = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if KEYWORD_RE.search(text) and len(NUMBER_RE.findall(text)) >= MIN_TABLE_NUMBERS:
            hits.append((len(NUMBER_RE.findall(text)), i))
    if not hits:
        return None, 0, total
    keep = sorted({k for _, i in hits for k in (i, i + 1) if k < total})
    if len(keep) > MAX_PAGES:
        # keep the densest tables first
        keep = set()
        for _, i in sorted(hits, reverse=True):
            if len(keep) >= MAX_PAGES:
                break
            keep.update(k for k in (i, i + 1) if k < total)
        keep = sorted(keep)[:MAX_PAGES]
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
    spans = []
    for m in matches:
        s, e = max(0, m.start() - 3_000), m.start() + 15_000
        if spans and s <= spans[-1][1]:
            spans[-1][1] = e
        else:
            spans.append([s, e])
    return "\n...\n".join(text[s:e] for s, e in spans)[:400_000]


def build_content(path: Path, log=print) -> list | None:
    """Claude content blocks, or None when the file has no revenue-breakdown tables (no call needed)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pdf, kept, total = select_segment_pages(path)
        if pdf is None:
            return None
        log(f"  {path.name}: segment pass sending {kept} of {total} pages")
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
    return [block, {"type": "text", "text": "Extract every revenue breakdown."}]


def _with_source(data: dict, doc: dict) -> dict:
    path = Path(doc["file"])
    source = {k: doc.get(k) for k in ("period_label", "period_end", "doc_type", "href", "file")}
    source["file_name"] = doc.get("file_name") or path.name
    if not source["period_end"]:
        source["period_end"], label = latest_period({"statements": data["breakdowns"]})
        source["period_label"] = label or source["file_name"]
    return {**data, "source": source}


def extract_segments_one(doc: dict, client=None, force: bool = False, log=print) -> dict:
    path = Path(doc["file"])
    name = doc.get("file_name") or doc["period_label"]
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    cache = EXTRACT_DIR / f"{file_hash(path)}_segments_v{SEGMENTS_PROMPT_VERSION}.json"
    if cache.exists() and not force:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return {**_with_source(data, doc), "cost_usd": 0.0, "cached": True}
    if path.suffix.lower() == ".xls":
        raise RuntimeError("Old .xls files aren't supported. Open it in Excel and save as .xlsx.")
    content = build_content(path, log)
    if content is None:
        data, cost = {"company": "", "currency": "", "breakdowns": []}, 0.0
        log(f"No revenue breakdown tables in {name}")
    else:
        client = client or get_client()
        log(f"Extracting revenue segments from {name}...")
        data, cost = call_json(client, content, SCHEMA, effort=EFFORT, system=SYSTEM)
        for b in data["breakdowns"]:
            n = len(b["columns"])
            for row in b["rows"]:
                row["values"] = (row["values"] + [None] * n)[:n]
        log(f"Done segments {name}: {', '.join(b['name'] for b in data['breakdowns']) or 'none'} (${cost:.2f})")
    cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return {**_with_source(data, doc), "cost_usd": cost, "cached": False}


def extract_many(docs: list[dict], force: bool = False, workers: int = 4, log=print) -> tuple[list[dict], list[dict]]:
    client = get_client()
    docs = [d for d in docs if d.get("file")]

    def work(d):
        try:
            return extract_segments_one(d, client, force, log)
        except Exception as e:
            log(f"FAILED segments {d['period_label']}: {e}")
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
    out = Path(args.documents_json).with_name("segments.json")
    out.write_text(json.dumps({"company": meta["company"], "extractions": ok, "failed": failed}, indent=1),
                   encoding="utf-8")
    print(json.dumps({"extracted": len(ok), "failed": len(failed),
                      "cost_usd": round(sum(r["cost_usd"] for r in ok), 2), "output": str(out)}))
