"""Extract forward-looking guidance (financial, operational, ESG; numeric and qualitative) from reports using Claude.

This is a fourth pass that runs alongside the statements, non-GAAP and segment passes. It has its own
prompt and cache (<hash>_guidance_v<N>.json), so the other passes are unaffected.

Guidance is mostly narrative and scattered, so instead of a filtered PDF this pass sends the document's
TEXT with [Page N] markers: the whole text when it is short enough (a results announcement is about
$0.10 of input), otherwise only pages with forward-looking wording plus their neighbours.

Usage:
    py tools/extract_guidance.py <documents.json> [--force] [--workers 4]

Writes guidance.json next to documents.json. Files without any forward-looking wording are skipped
without a Claude call (cost $0) and cached as empty.
"""
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import EXTRACT_DIR, call_json, get_client
from extract_financials import PROMPT_VERSION, _sheet_text, file_hash, latest_period

GUIDANCE_PROMPT_VERSION = 1
EFFORT = "medium"
MAX_CHARS = 300_000  # ~75k tokens; above this only forward-looking pages are sent

KEYWORD_RE = re.compile(
    r"guidance|outlook|\bexpect(s|ed)? (to|that)|we expect|anticipat|\btarget|forecast|will strive|"
    r"aim(s|ing)? to|plan(s|ning)? to|intend(s)? to|estimate[sd]? (to|that)|by (the end of )?20\d\d|"
    r"\bwe will\b|going forward|moving forward|looking ahead|in the future|"
    r"(next|coming) (\w+ )?(year|quarter|five years|three years)s?|remain committed",
    re.I,
)
BOILERPLATE_RE = re.compile(
    r"expected credit loss\w*|expected useful li(fe|ves)|estimated useful li(fe|ves)|"
    r"forward[- ]looking statements?|business review and outlook",
    re.I,
)

ITEM = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["financial", "operational", "esg", "other"]},
        "metric": {"type": "string"},
        "segment": {"type": "string"},
        "guided_period": {"type": "string"},
        "period_end": {"type": "string"},
        "type": {"type": "string", "enum": ["range", "point", "minimum", "maximum", "growth", "qualitative"]},
        "low": {"type": ["number", "null"]},
        "high": {"type": ["number", "null"]},
        "unit": {"type": "string"},
        "basis": {"type": "string"},
        "status": {"type": "string", "enum": ["new", "raised", "lowered", "reiterated", "achieved", ""]},
        "quote": {"type": "string"},
        "page": {"type": ["integer", "null"]},
    },
    "required": ["category", "metric", "segment", "guided_period", "period_end", "type", "low", "high", "unit",
                 "basis", "status", "quote", "page"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "items": {"type": "array", "items": ITEM},
    },
    "required": ["company", "items"],
    "additionalProperties": False,
}

SYSTEM = """You extract a company's forward-looking guidance from a financial report into JSON. The report text is given with [Page N] markers.

Capture EVERY forward-looking statement the company makes about its OWN future:
- financial: revenue, profit, margins, EPS, capex, R&D or other spending, dividends, buybacks, tax rate...
- operational: deliveries, shipments, store openings, capacity, launches with dates, user or headcount targets...
- esg: emissions, energy, recycling and other sustainability targets.
- other: any remaining forward-looking target or plan.
Include qualitative outlook statements without numbers too (e.g. "we expect gross margin to remain under pressure"), with type "qualitative" and low/high null.
Skip: historical results, forecasts by third parties (IMF, Omdia, industry data), forward-looking-statement disclaimers, accounting estimates and statements about new accounting standards, and vague slogans with no forward-looking content.

For each item:
- metric: a short, consistent name ("EV deliveries", "R&D expenses", "Overseas Mi Home stores", "Gross margin", "Revenue"). Use the same name for the same target every time so revisions line up across reports.
- segment: the business segment or product it concerns, or "".
- guided_period: the period it refers to as a label ("FY2026", "Q3 2026", "2026-2030", "by 2040", "next five years"); period_end: its end date YYYY-MM-DD if determinable, else "".
- type: range (low-high), point (a single figure: low = high), minimum ("exceed", "at least", "over": low only), maximum ("up to", "no more than": high only), growth (a growth rate or % change: low/high in %), qualitative.
- low/high: numbers exactly as printed, without rescaling; unit says what they are ("RMB billion", "vehicles", "%", "stores", "tons").
- basis: what the number measures, e.g. "absolute", "YoY growth", "cumulative", "non-IFRS", "vs 2020 baseline"; "" if nothing specific.
- status: only if the report says so: "raised" / "lowered" (a new target replacing an earlier one), "reiterated", "achieved" (a past target reported as met, with the original target as the value), "new"; else "".
- quote: the statement verbatim (one or two sentences).
- page: the N of the [Page N] marker where the statement appears.
Accuracy matters more than anything else: quote exactly and never infer numbers that are not stated. If there is no guidance, return an empty items list."""


def _clean(text: str) -> str:
    return " ".join(text.split())


def pdf_pages(path: Path) -> list[str]:
    from pypdf import PdfReader

    out = []
    for page in PdfReader(str(path)).pages:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            out.append("")
    return out


def keyword_hits(pages: list[str]) -> list[int]:
    """Pages with forward-looking wording (English), boilerplate excluded."""
    return [i for i, t in enumerate(pages) if KEYWORD_RE.search(BOILERPLATE_RE.sub(" ", _clean(t)))]


def select_guidance_text(pages: list[str], hits: list[int] | None = None) -> tuple[str | None, int]:
    """Text with [Page N] markers: all pages if short enough, else forward-looking pages +-1. None if no match."""
    hits = keyword_hits(pages) if hits is None else hits
    if not hits:
        return None, 0
    if sum(len(t) for t in pages) <= MAX_CHARS:
        keep = list(range(len(pages)))
    else:
        keep = sorted({k for i in hits for k in (i - 1, i, i + 1) if 0 <= k < len(pages)})
    parts, size, kept = [], 0, 0
    for k in keep:
        chunk = f"[Page {k + 1}]\n{pages[k].strip()}\n"
        if size + len(chunk) > MAX_CHARS:
            break
        parts.append(chunk)
        size += len(chunk)
        kept += 1
    return "\n".join(parts), kept


def build_content(path: Path, log=print, client=None) -> tuple[list | None, float]:
    """(Claude content blocks or None when the file has no forward-looking content, page-finder cost).

    Text files: the text with [Page N] markers (English: keyword pages decide; other languages: the AI page
    finder's outlook pages decide when the text is too long to send whole). Scanned PDFs: the page images of
    the pages the AI page finder marks as outlook / guidance."""
    import page_finder as pf

    if path.suffix.lower() not in (".pdf", ".htm", ".html", ".xlsx", ".xlsm", ".csv"):
        raise RuntimeError(f"Unsupported file type: {path.suffix.lower()}")
    pages = pf.page_texts(path)
    use_ai, profile = pf.needs_ai(path)
    note = pf.language_note(profile)
    instruction = "Extract all forward-looking guidance."
    if path.suffix.lower() == ".pdf" and profile["scanned"]:
        found = pf.find_pages(path, "guidance", client, log=log)
        if not found["pages"]:
            return None, found["cost_usd"]
        log(f"  {path.name}: guidance pass sending {len(found['pages'])} scanned pages")
        block = pf._pdf_block(pf.pdf_subset(path, found["pages"]))
        instruction += (" The report is scanned: give page as the number printed on the page, or its position "
                        "in this PDF if none is printed.")
        return [block, {"type": "text", "text": instruction + note}], found["cost_usd"]
    cost = 0.0
    hits = keyword_hits(pages)
    if profile["language"] != "English":
        if sum(len(t) for t in pages) <= MAX_CHARS:
            hits = list(range(len(pages)))  # short enough to send whole; English keywords don't apply
        else:
            found = pf.find_pages(path, "guidance", client, log=log)
            hits, cost = found["pages"], found["cost_usd"]
    text, kept = select_guidance_text(pages, hits)
    if text is None:
        return None, cost
    log(f"  {path.name}: guidance pass sending text of {kept} of {len(pages)} pages")
    block = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text}}
    return [block, {"type": "text", "text": instruction + note}], cost


def _issuing_period(doc: dict, path: Path) -> tuple[str, str]:
    """(period_end, label) of the report: from doc (URL mode), else from its cached statements extraction."""
    if doc.get("period_end"):
        return doc["period_end"], doc.get("period_label") or ""
    cache = EXTRACT_DIR / f"{file_hash(path)}_v{PROMPT_VERSION}.json"
    if cache.exists():
        try:
            return latest_period(json.loads(cache.read_text(encoding="utf-8")))
        except Exception:
            pass
    return "", ""


def _with_source(data: dict, doc: dict) -> dict:
    path = Path(doc["file"])
    source = {k: doc.get(k) for k in ("period_label", "period_end", "doc_type", "href", "file")}
    source["file_name"] = doc.get("file_name") or path.name
    source["period_end"], label = _issuing_period(doc, path)
    source["period_label"] = label or source["file_name"]
    return {**data, "source": source}


def extract_guidance_one(doc: dict, client=None, force: bool = False, log=print) -> dict:
    path = Path(doc["file"])
    name = doc.get("file_name") or doc["period_label"]
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    cache = EXTRACT_DIR / f"{file_hash(path)}_guidance_v{GUIDANCE_PROMPT_VERSION}.json"
    if cache.exists() and not force:
        data = json.loads(cache.read_text(encoding="utf-8"))
        import page_finder as pf

        # an empty result from before text normalisation / scanned / non-English support: look again if warranted
        if (data["items"] or data.get("page_rules", 0) >= pf.PAGE_RULES_VERSION
                or not (keyword_hits(pf.page_texts(path)) or pf.needs_ai(path)[1]["reason"])):
            return {**_with_source(data, doc), "cost_usd": 0.0, "cached": True}
        log(f"  {name}: earlier run found no guidance; looking again")
    if path.suffix.lower() == ".xls":
        raise RuntimeError("Old .xls files aren't supported. Open it in Excel and save as .xlsx.")
    client = client or get_client()
    content, finder_cost = build_content(path, log, client)
    if content is None:
        data, cost = {"company": "", "items": []}, finder_cost
        log(f"No forward-looking statements in {name}")
    else:
        log(f"Extracting guidance from {name}...")
        data, cost = call_json(client, content, SCHEMA, effort=EFFORT, system=SYSTEM)
        cost += finder_cost
        log(f"Done guidance {name}: {len(data['items'])} items (${cost:.2f})")
    from page_finder import PAGE_RULES_VERSION

    data["page_rules"] = PAGE_RULES_VERSION  # an empty result made with these rules is final
    cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return {**_with_source(data, doc), "cost_usd": cost, "cached": False}


def extract_many(docs: list[dict], force: bool = False, workers: int = 4, log=print) -> tuple[list[dict], list[dict]]:
    client = get_client()
    docs = [d for d in docs if d.get("file")]

    def work(d):
        try:
            return extract_guidance_one(d, client, force, log)
        except Exception as e:
            log(f"FAILED guidance {d['period_label']}: {e}")
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
    out = Path(args.documents_json).with_name("guidance.json")
    out.write_text(json.dumps({"company": meta["company"], "extractions": ok, "failed": failed}, indent=1),
                   encoding="utf-8")
    print(json.dumps({"extracted": len(ok), "failed": len(failed),
                      "cost_usd": round(sum(r["cost_usd"] for r in ok), 2), "output": str(out)}))
