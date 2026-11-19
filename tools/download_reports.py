"""Discover financial reports on an investor-relations page and download them.

Usage:
    py tools/download_reports.py <ir_page_url> [--years 3] [--force]

Writes .tmp/reports/<site>/documents.json listing every downloaded file.
"""
import argparse
import base64
import json
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from common import REPORTS_DIR, call_json, get_client, safe_filename, site_slug

DOC_HINT = re.compile(
    r"\.(pdf|xlsx?|csv|html?)(\?|$)|static-files|/download|attachment|"
    r"result|report|announcement|financial|statement|10-?k|10-?q|20-?f|interim|annual|quarter",
    re.I,
)
SKIP_HINT = re.compile(r"^(mailto:|tel:|javascript:)|webcast|media-server|youtube|linkedin|twitter|facebook", re.I)
BLOCKED_TITLES = ("access denied", "forbidden", "attention required", "just a moment")

COLLECT_JS = """
anchors => anchors.map(a => {
  const ctx = [];
  let n = a.parentElement, depth = 0;
  while (n && depth < 20 && ctx.length < 6) {
    for (const c of n.childNodes) {
      if (c.nodeType === 3 && c.textContent.trim()) ctx.push(c.textContent.trim().slice(0, 60));
    }
    n = n.parentElement; depth++;
  }
  const heads = [];
  for (const h of document.querySelectorAll('h1,h2,h3,h4,h5')) {
    if (h.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING) heads.push(h.innerText.trim().slice(0, 80));
  }
  return {
    text: (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
    title: a.getAttribute('title') || '',
    type: a.getAttribute('type') || '',
    href: a.href,
    context: ctx.join(' | '),
    headings: heads.slice(-3).join(' > '),
  };
})
"""


def _blocked(page, resp) -> bool:
    status = resp.status if resp else 0
    return status in (401, 403, 429) or page.title().strip().lower() in BLOCKED_TITLES


@contextmanager
def open_site(url: str, log=print):
    """Open the page in Chromium. Tries headless first; many IR sites (Akamai) block it, so falls back to a visible window."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        for headless in (True, False):
            browser = p.chromium.launch(headless=headless)
            ctx = browser.new_context(locale="en-US", viewport={"width": 1400, "height": 1000}, accept_downloads=True)
            page = ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            if not _blocked(page, resp):
                log(f"Opened page ({'headless' if headless else 'visible'} browser): {page.title()}")
                try:
                    yield page
                finally:
                    browser.close()
                return
            log(f"{'Headless' if headless else 'Visible'} browser was blocked (HTTP {resp.status if resp else '?'}).")
            browser.close()
        raise RuntimeError("The site blocked automated access even with a visible browser window.")


def _expand_page(page):
    """Click accordion/tab toggles and scroll so lazily rendered links appear."""
    start_url = page.url
    for _ in range(6):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(400)
    toggles = page.query_selector_all(
        "main [aria-expanded='false']:not(a[href]:not([href^='#'])), "
        "main button[class*='more' i], .view-content [aria-expanded='false']:not(a[href]:not([href^='#']))"
    )
    for el in toggles[:60]:
        try:
            if el.evaluate("e => !!e.closest('nav, header, [role=navigation], [role=menu]')"):
                continue
            el.click(timeout=1000)
        except Exception:
            pass
        if page.url.split("#")[0] != start_url.split("#")[0]:
            page.go_back(wait_until="domcontentloaded")
    page.wait_for_timeout(1000)


def collect_links(page) -> list[dict]:
    _expand_page(page)
    links = page.eval_on_selector_all("a[href]", COLLECT_JS)
    seen, out = set(), []
    for link in links:
        href = link["href"].split("#")[0]
        blob = " ".join([href, link["text"], link["title"], link["type"]])
        if not href or href in seen or SKIP_HINT.search(href) or not DOC_HINT.search(blob):
            continue
        if href.rstrip("/") == page.url.split("#")[0].rstrip("/"):
            continue
        seen.add(href)
        out.append({**link, "href": href})
    return out


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "doc_type": {
                        "type": "string",
                        "enum": ["quarterly_results", "interim_results", "annual_results", "annual_report",
                                 "interim_report", "quarterly_report", "financial_statements"],
                    },
                    "period_label": {"type": "string"},
                    "period_end": {"type": "string"},
                },
                "required": ["index", "doc_type", "period_label", "period_end"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["company", "documents"],
    "additionalProperties": False,
}

CLASSIFY_SYSTEM = """You classify links scraped from a company's investor-relations page.
Select the documents that contain the company's full primary financial statements (income statement / profit or loss, balance sheet / financial position, cash flow statement) for a reporting period: quarterly, interim or annual results announcements, 10-Q/10-K/20-F filings, quarterly/interim/annual reports, or standalone financial statements.
Exclude presentations, slides, webcasts, transcripts, press releases without full statements, ESG reports, circulars and anything else.
Choose ONE document per reporting period. If a period has both a results announcement and a full annual/interim report, choose the results announcement (it is shorter and has the same statements). Prefer English versions.
For each selected document give: index (from the input list), doc_type, period_label in the company's fiscal convention (e.g. "2025 Q2", "FY2024", "H1 2025"), and period_end as YYYY-MM-DD (the last day of the period covered; use your best inference from headings, link text and file names).
Use the year headings, quarter labels and file names in each link's context to work out the period."""


def classify(candidates: list[dict], client=None) -> tuple[dict, float]:
    client = client or get_client()
    lines = [
        f"[{i}] text={c['text']!r} title={c['title']!r} file={unquote(urlparse(c['href']).path.rsplit('/', 1)[-1])!r} "
        f"type={c['type']!r} context={c['context']!r} headings={c['headings']!r}"
        for i, c in enumerate(candidates)
    ]
    content = "Links found on the page:\n" + "\n".join(lines)
    result, cost = call_json(client, content, CLASSIFY_SCHEMA, effort="low", system=CLASSIFY_SYSTEM, max_tokens=32000)
    docs = []
    for d in result["documents"]:
        if 0 <= d["index"] < len(candidates):
            docs.append({**d, **{k: candidates[d["index"]][k] for k in ("href", "text", "title")}})
    docs.sort(key=lambda d: d["period_end"], reverse=True)
    return {"company": result["company"], "documents": docs}, cost


def filter_years(docs: list[dict], years: int | None) -> list[dict]:
    if not years:
        return docs
    ends = [d["period_end"][:4] for d in docs if re.match(r"\d{4}", d["period_end"])]
    if not ends:
        return docs
    min_year = int(max(ends)) - years + 1
    return [d for d in docs if not re.match(r"\d{4}", d["period_end"]) or int(d["period_end"][:4]) >= min_year]


FETCH_JS = """
async url => {
  const r = await fetch(url, {credentials: 'include'});
  const buf = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return {status: r.status, type: r.headers.get('content-type') || '', b64: btoa(s)};
}
"""

EXT_BY_TYPE = {"pdf": ".pdf", "spreadsheetml": ".xlsx", "ms-excel": ".xls", "html": ".html", "csv": ".csv"}


def _fetch(page, url: str) -> tuple[bytes, str]:
    try:
        r = page.context.request.get(url, timeout=120000)
        ctype = r.headers.get("content-type", "")
        # an HTML response for a non-HTML link is usually a bot-block page
        if r.ok and ("text/html" not in ctype or url.lower().endswith((".htm", ".html"))):
            return r.body(), ctype
    except Exception:
        pass
    res = page.evaluate(FETCH_JS, url)
    if res["status"] >= 400:
        raise RuntimeError(f"HTTP {res['status']}")
    return base64.b64decode(res["b64"]), res["type"]


def download(page, docs: list[dict], site_dir: Path, force: bool = False, log=print) -> list[dict]:
    site_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = site_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    out = []
    for d in docs:
        cached = manifest.get(d["href"])
        if cached and not force and (site_dir / cached).exists():
            log(f"Cached: {d['period_label']} -> {cached}")
            out.append({**d, "file": str(site_dir / cached)})
            continue
        try:
            data, ctype = _fetch(page, d["href"])
        except Exception as e:
            log(f"FAILED {d['period_label']}: {e}")
            out.append({**d, "file": None, "error": str(e)})
            continue
        ext = next((v for k, v in EXT_BY_TYPE.items() if k in ctype.lower()), None)
        if data[:4] == b"%PDF":
            ext = ".pdf"
        ext = ext or Path(urlparse(d["href"]).path).suffix or ".bin"
        name = safe_filename(f"{d['period_end']}_{d['period_label']}_{d['doc_type']}") + ext
        (site_dir / name).write_bytes(data)
        manifest[d["href"]] = name
        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        log(f"Downloaded {d['period_label']} ({len(data) / 1e6:.1f} MB) -> {name}")
        out.append({**d, "file": str(site_dir / name)})
    return out


def discover(url: str, client=None, log=print) -> tuple[dict, list[dict], float]:
    with open_site(url, log) as page:
        candidates = collect_links(page)
    log(f"Found {len(candidates)} candidate document links; asking Claude which ones are financial reports...")
    if not candidates:
        raise RuntimeError("No document links found on the page.")
    result, cost = classify(candidates, client)
    log(f"{result['company']}: {len(result['documents'])} financial-statement documents identified.")
    return result, candidates, cost


def run(url: str, years: int | None = 3, force: bool = False, log=print) -> dict:
    site_dir = REPORTS_DIR / site_slug(url)
    result, _, cost = discover(url, log=log)
    docs = filter_years(result["documents"], years)
    with open_site(url, log) as page:
        docs = download(page, docs, site_dir, force=force, log=log)
    out = {"url": url, "company": result["company"], "documents": docs, "classify_cost_usd": cost}
    (site_dir / "documents.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--years", type=int, default=3, help="most recent N fiscal years (0 = all)")
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    args = ap.parse_args()
    res = run(args.url, args.years or None, args.force)
    print(json.dumps({"company": res["company"], "downloaded": sum(1 for d in res["documents"] if d.get("file"))}))
