"""Extract income statement, balance sheet and cash flow from downloaded reports using Claude.

Usage:
    py tools/extract_financials.py <documents.json> [--force] [--workers 4]

documents.json is produced by download_reports.py. Each extraction is cached in
.tmp/extractions/ keyed by file hash, so re-runs cost nothing unless --force.

Pages are chosen by page_finder.py (rules, plus Claude Haiku when needed). After extraction, checks.py
verifies the statements; if one is missing or its totals don't add up, the extraction is retried once
with the AI page finder's pages added, and the better result is kept.
"""
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import EXTRACT_DIR, call_json, file_hash, get_client

PROMPT_VERSION = 1
EFFORT = "medium"
UNIT_MULTIPLIERS = [1, 1000, 10000, 100000, 1000000, 10000000, 100000000, 1000000000]

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
                    "unit_multiplier": {"type": "integer", "enum": UNIT_MULTIPLIERS},
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

Numbers: transcribe as plain numbers in the statement's own unit, without rescaling. Parentheses or a leading minus mean negative. A dash or blank means null. unit_label is the unit as stated (e.g. "RMB million"); unit_multiplier is its scale (1, 1000, 1000000 or 1000000000; also 100000 for lakh, 10000000 for crore, 10000 for 万, 100000000 for 億/亿). Set unscaled=true on rows whose values are not in that unit: per-share amounts, share counts, percentages.
Accuracy matters more than anything else: copy each figure exactly."""


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


def build_content(path: Path, log=print, client=None, need_ai: bool = False, extra_pages=()) -> tuple[list, dict]:
    """(content blocks, page info). Page info has 'pages' (0-based, sent), 'total', 'profile', 'ai', 'cost_usd'."""
    from page_finder import content_block, language_note

    if path.suffix.lower() not in (".pdf", ".htm", ".html", ".xlsx", ".xlsm", ".csv"):
        raise RuntimeError(f"Unsupported file type: {path.suffix.lower()}")
    block, info = content_block(path, "statements", client, need_ai, log, extra_pages, whole_if_none=True)
    if path.suffix.lower() == ".pdf":
        log(f"  {path.name}: sending {len(info['pages'])} of {info['total']} pages"
            + (" (AI page finder used)" if info["ai"] else ""))
    text = ("Extract the income statement, balance sheet and cash flow statement. If the document holds several "
            "sets of statements (consolidated and standalone / parent company, or the same statements under another "
            "accounting standard or currency), use the consolidated set in the currency of the company's main "
            "financial statements, and take all three statements from that same set." + language_note(info["profile"]))
    return [block, {"type": "text", "text": text}], info


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
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("statements"):
            log(f"Cached extraction: {name}")
            if "checks" not in data:  # cached before checks existed: compute them (free)
                from checks import check_statements

                data["checks"] = check_statements(data)
            return {**_with_source(data, doc), "cost_usd": 0.0, "cached": True}
        # an earlier run found no statements at all (e.g. wrong pages were sent): try again rather than reuse it
        log(f"Previous extraction of {name} found no statements; extracting again")
    if path.suffix.lower() == ".xls":
        raise RuntimeError("Old .xls files aren't supported. Open it in Excel and save as .xlsx.")
    from checks import better, check_statements, needs_retry, summary

    client = client or get_client()
    log(f"Extracting {name}...")
    content, info = build_content(path, log, client)
    data, cost = _extract(client, content)
    cost += info["cost_usd"]
    data["checks"] = check_statements(data)
    data["pages_sent"] = [p + 1 for p in info["pages"]]
    retried = False

    # retry once when a statement is missing or does not add up, with the AI page finder's pages added
    if needs_retry(data["checks"]) and path.suffix.lower() in (".pdf", ".htm", ".html"):
        s = summary(data["checks"])
        log(f"  {name}: {s['missing']} statement(s) missing, {s['fail']} check(s) failed; retrying with the AI page finder")
        # statements sit together: when one is missing, also send the pages around the ones already found
        near = set()
        if s["missing"] and path.suffix.lower() == ".pdf" and len(info["pages"]) < info["total"]:
            near = {p + k for p in info["pages"] for k in (-3, -2, -1, 1, 2, 3)} - set(info["pages"])
        content2, info2 = build_content(path, log, client, need_ai=True, extra_pages=set(info["pages"]) | near)
        cost += info2["cost_usd"]
        if set(info2["pages"]) != set(info["pages"]) or not info["ai"]:
            data2, cost2 = _extract(client, content2)
            cost += cost2
            retried = True
            data2["checks"] = check_statements(data2)
            data2["pages_sent"] = [p + 1 for p in info2["pages"]]
            if better(data["checks"], data2["checks"]):
                data = data2
                log(f"  {name}: retry kept (better checks)")
            else:
                log(f"  {name}: retry was not better; kept the first result")
        else:
            log(f"  {name}: the AI page finder found no other pages; kept the first result")
    data["retried"] = retried
    cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    data = _with_source(data, doc)
    found = ", ".join(s["statement"] for s in data["statements"]) or "none"
    log(f"Done {name} -> {data['source']['period_label']}: {found} (${cost:.2f})")
    return {**data, "cost_usd": cost, "cached": False}


def _extract(client, content) -> tuple[dict, float]:
    data, cost = call_json(client, content, SCHEMA, effort=EFFORT, system=SYSTEM)
    for st in data["statements"]:
        n = len(st["columns"])
        for row in st["rows"]:
            row["values"] = (row["values"] + [None] * n)[:n]
    return data, cost


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
