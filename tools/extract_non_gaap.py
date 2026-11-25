"""Extract GAAP -> non-GAAP reconciliations (adjusted / non-IFRS measures) from reports using Claude.

This is a second pass that runs alongside extract_financials.py. It has its own page filter, prompt and
cache (<hash>_nongaap_v<N>.json), so the primary-statement extraction is unaffected.

Usage:
    py tools/extract_non_gaap.py <documents.json> [--force] [--workers 4]

Writes non_gaap.json next to documents.json. Files without any non-GAAP wording are skipped
without a Claude call (cost $0) and cached as empty.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import EXTRACT_DIR, call_json, file_hash, get_client
from extract_financials import UNIT_MULTIPLIERS, latest_period

NON_GAAP_PROMPT_VERSION = 1
EFFORT = "medium"

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
                    "unit_multiplier": {"type": "integer", "enum": UNIT_MULTIPLIERS},
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

Numbers: plain numbers in the table's own unit, no rescaling. Parentheses or a leading minus mean negative; keep each adjustment's sign as printed. A dash or blank means null. unit_label is the unit as stated (e.g. "RMB in thousand"); unit_multiplier its scale (100000 for lakh, 10000000 for crore, 10000 for 万, 100000000 for 億/亿). Set unscaled=true on per-share amounts, share counts and percentages.

Use only reconciliation tables (and non-GAAP highlight tables that show both bases side by side if no reconciliation exists). Ignore the primary financial statements and narrative text. If the document has no non-GAAP measures, return an empty reconciliations list.
Accuracy matters more than anything else: copy each figure exactly."""


def build_content(path: Path, log=print, client=None) -> tuple[list | None, dict]:
    """(Claude content blocks or None when the file has no non-GAAP / APM tables, page info)."""
    from page_finder import content_block, language_note

    block, info = content_block(path, "non_gaap", client, log=log)
    if block is None:
        return None, info
    if path.suffix.lower() == ".pdf":
        log(f"  {path.name}: non-GAAP pass sending {len(info['pages'])} of {info['total']} pages")
    text = "Extract every GAAP to non-GAAP reconciliation." + language_note(info["profile"])
    return [block, {"type": "text", "text": text}], info


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
        from page_finder import PAGE_RULES_VERSION, has_candidates

        # an empty result from an older, narrower page filter: look again when pages are now found
        if (data["reconciliations"] or data.get("page_rules", 0) >= PAGE_RULES_VERSION
                or not has_candidates(path, "non_gaap")):
            return {**_with_source(data, doc), "cost_usd": 0.0, "cached": True}
        log(f"  {name}: earlier run found no non-GAAP measures; the page finder now finds candidate pages")
    if path.suffix.lower() == ".xls":
        raise RuntimeError("Old .xls files aren't supported. Open it in Excel and save as .xlsx.")
    content, info = build_content(path, log, client)
    if content is None:
        data, cost = _empty(), 0.0
        log(f"No non-GAAP measures mentioned in {name}")
    else:
        client = client or get_client()
        log(f"Extracting non-GAAP measures from {name}...")
        data, cost = call_json(client, content, SCHEMA, effort=EFFORT, system=SYSTEM)
        cost += info["cost_usd"]
        for rec in data["reconciliations"]:
            n = len(rec["columns"])
            for row in rec["rows"]:
                row["values"] = (row["values"] + [None] * n)[:n]
        log(f"Done non-GAAP {name}: {len(data['reconciliations'])} reconciliations (${cost:.2f})")
    from page_finder import PAGE_RULES_VERSION

    data["page_rules"] = PAGE_RULES_VERSION  # an empty result made with these rules is final
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
