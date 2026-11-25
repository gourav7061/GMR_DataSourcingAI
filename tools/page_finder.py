"""Find the pages that hold each kind of table in any company's report.

Used by all four extraction passes (statements, non-GAAP, segments, guidance):

1. Rules (free): every page's text is normalised (ligatures, tabs, odd hyphens, letter-spaced titles) and scored
   by its CONTENT - statement titles plus the line items a statement always has ("total assets",
   "operating activities", ...) and how many numbers it holds. Contents pages are ignored.
2. Claude Haiku 5.5 (only when needed, about $0.01 per report): when the report is scanned, not in English,
   the rules could not find one of the three statements, or a statement extraction failed its checks.
   It reads each page's text (or the page images of a scanned PDF) and lists the pages per category.
   The result is cached in .tmp/extractions/<hash>_pages_v1.json, shared by all passes.

Usage (diagnostics, free unless --ai):
    py tools/page_finder.py <file> [--ai]
"""
import io
import json
import re
import threading
import unicodedata
from pathlib import Path

from common import EXTRACT_DIR, LOCATOR_MODEL, call_json, file_hash

PAGES_VERSION = 1
# bump when the rules find materially more pages: earlier EMPTY non-GAAP / segment / guidance results made with older
# rules then get one more look (results that found something are never redone)
PAGE_RULES_VERSION = 1
CATEGORIES = ["income_statement", "balance_sheet", "cash_flow", "non_gaap", "segments", "guidance"]
STATEMENTS = ["income_statement", "balance_sheet", "cash_flow"]
STATEMENT_PAGE_CAP = 24
TABLE_PAGE_CAP = 30
HTML_SECTION = 5_000
NUM_MIN = 15  # numbers on a page before it counts as a statement table

# ---------- text normalisation ----------
_HYPHENS = dict.fromkeys(map(ord, "‐‑‒–—―−﹣－"), "-")
_HYPHENS.update({ord("­"): None, ord("\t"): " ", ord(" "): " ", ord(" "): " ", ord(" "): " "})


def norm(text: str) -> str:
    """Readable text: NFKC (ligature characters become letters), one kind of hyphen, single spaces."""
    text = unicodedata.normalize("NFKC", text or "").translate(_HYPHENS)
    return "\n".join(re.sub(r" {2,}", " ", line).strip() for line in text.splitlines())


def soft(text: str) -> str:
    """For keyword search: lower case, fi/fl folded to f (pypdf often drops the ligature), hyphens as spaces."""
    t = norm(text).lower().replace("fi", "f").replace("fl", "f")
    return re.sub(r"[\s\-]+", " ", t)


def fold(text: str) -> str:
    """For title search: like soft() but without any spaces or punctuation (catches 'B A L A N C E  S H E E T')."""
    return re.sub(r"[^a-z0-9]+", "", soft(text))


def _soft_re(*patterns: str) -> re.Pattern:
    """Compile plain lower-case English patterns to match soft() text. A space in a pattern matches any spacing."""
    out = []
    for p in patterns:
        p = p.replace("fi", "f").replace("fl", "f").replace("-", " ")
        out.append(re.sub(r" ", r"\\s?", p))
    return re.compile("|".join(f"(?:{p})" for p in out))


def _fold_re(pattern: str) -> re.Pattern:
    return re.compile(pattern.replace("fi", "f").replace("fl", "f").replace(" ", "").replace("-", ""))


# ---------- what each category looks like ----------
TITLES = {
    "income_statement": _fold_re(
        r"(statements? of (consolidated )?(profit or loss|comprehensive (income|loss)|income|operations|earnings|"
        r"financial performance)|income statements?|profit and loss)"),
    "balance_sheet": _fold_re(r"(balance sheets?|statements? of (consolidated )?financial (position|condition))"),
    "cash_flow": _fold_re(r"(statements? of (consolidated )?cash flows?|cash flows? statements?)"),
}
ANCHORS = {
    "income_statement": [_soft_re(p) for p in (
        r"\brevenues?\b|\bturnover\b|net sales|total income|net interest income",
        r"cost of (sales|revenues?|goods sold)",
        r"gross (profit|margin)",
        r"operating (profit|income|loss)|(profit|income|loss) from operations",
        r"(profit|income|loss|earnings) before (income )?tax",
        r"income tax",
        r"net (income|profit|loss|earnings)|(profit|loss) for the (period|year)|(profit|loss) attributable",
        r"per share",
    )],
    "balance_sheet": [_soft_re(p) for p in (
        r"total assets",
        r"total liabilities",
        r"total (shareholders'? |stockholders'? )?equity|equity attributable",
        r"\bcurrent assets",
        r"non ?current assets|noncurrent assets",
        r"current liabilities",
        r"cash and cash equivalents",
        r"retained (earnings|profits)|accumulated (deficit|losses)|share capital|reserves",
        r"property,? plant and equipment|inventories|goodwill",
    )],
    "cash_flow": [_soft_re(p) for p in (
        r"operating activities",
        r"investing activities",
        r"financing activities",
        r"(beginning|end) of (the )?(period|year|quarter)|at (the )?(beginning|end)",
        r"net (increase|decrease|change)",
        r"depreciation",
        r"(purchases?|acquisitions?) of property|capital expenditure|dividends paid",
    )],
}
# strong wording names the measures explicitly; broad wording (EBITDA, net debt...) also appears in ordinary
# commentary, so a page with only broad wording counts only when it also holds a table
NON_GAAP_STRONG_RE = _soft_re(
    r"non ?(gaap|ifrs|hkfrs|ind as)",
    r"alternative performance (measures?|indicators?)", r"\bapms?\b",
    r"adjusted (net|ebitda|ebita|ebit|operating|earnings|profit|diluted|basic|eps|income|gross|free)",
    r"underlying (profit|earnings|operating|ebit|eps|income|net)",
    r"core (earnings|profit|eps|operating|net)",
    r"normali[sz]ed (earnings|profit|ebitda|eps|net)", r"headline earnings", r"cash earnings",
    r"excluding (special|exceptional|one off|non recurring|significant) items",
    r"reconciliation of (gaap|ifrs|non)",
)
NON_GAAP_BROAD_RE = _soft_re(
    r"recurring (operations|operating|profit|net)", r"organic (growth|revenue|sales|change)",
    r"\bebitda\b", r"\bebita\b", r"free cash flow", r"net (financial )?debt",
    r"reconciliation (of|to|from|between)",
)
SEGMENT_RE = _soft_re(
    r"segment", r"revenues? by", r"by (geograph|region|product|channel|market|division|business|customer)",
    r"disaggregat", r"breakdown of (revenue|sales|turnover)", r"geographic(al)? (information|area|breakdown)",
    r"(revenue|sales|turnover) (analysis|breakdown)", r"sales by",
)
# companies that never say "segment": divisions / business units, or a regional split (e.g. Sartorius KPI tables).
# These words also appear in ordinary text, so they count only on dense table pages.
SEGMENT_SOFT_RE = _soft_re(
    r"\bdivisions?\b", r"business (units?|areas?|lines?|groups?)", r"(customers'?|customer) location",
)
SEGMENT_SOFT_MIN_NUMBERS = 20
# a table naming two or more regions is a geographic breakdown even without any segment wording
REGION_RE = _soft_re(
    r"\bemea\b", r"\bamericas\b", r"asia ?(\||/|and )? ?pacific", r"\bapac\b", r"\blatam\b", r"north america",
    r"latin america", r"\beurope\b", r"greater china", r"rest of (the )?world", r"middle east", r"\bjapan\b",
    r"chinese mainland|mainland china", r"\bindia\b", r"united states|\bu\.?s\.?\b",
)
RECONCIL_RE = re.compile(r"reconcil")
NUMBER_RE = re.compile(r"(?<![\w.])\(?-?\d{1,3}(?:[,. ]\d{3})+(?:[.,]\d+)?\)?|(?<![\w.,])\(?-?\d+(?:[.,]\d+)?%?\)?")
TOC_LINE_RE = re.compile(r"^\D{4,}\s\.*\s*\d{1,3}$")


YEAR_RE = re.compile(r"^\(?(19[89]\d|20[0-4]\d)\)?$")


def numbers(text: str) -> int:
    return len(NUMBER_RE.findall(text))


def table_numbers(text: str) -> int:
    """Numbers that look like table figures: grouped (1,234 / 1.234 / 1 234), decimals or 3+ digits; not years."""
    n = 0
    for tok in NUMBER_RE.findall(text):
        digits = re.sub(r"\D", "", tok)
        if YEAR_RE.match(tok.strip()):
            continue
        if len(digits) >= 3 or re.search(r"\d[.,]\d", tok):
            n += 1
    return n


DOT_LEADER_RE = re.compile(r"(\.{5,}|…{2,}|(\. ){4,})\s*\d{1,3}$")
TOKEN_RE = re.compile(r"\S+")
WORDISH_RE = re.compile(r"^[(\"'‘“]?[^\W\d_]+(?:['’\-][^\W\d_]+)*[)\"'’”.,:;!?%]*$")


def _is_toc(lines: list[str]) -> bool:
    """A contents page: many lines that are text followed by a page number, or dotted-leader lines."""
    lines = [ln for ln in lines if ln.strip()]
    if sum(1 for ln in lines if DOT_LEADER_RE.search(ln)) >= 3:
        return True
    toc = sum(1 for ln in lines if TOC_LINE_RE.match(ln))
    return toc >= 8 and toc >= 0.4 * len(lines)


def readable(text: str) -> bool:
    """False for pages without usable text: image-only, or a scrambled font encoding (text like '-AF3>B7DEA@3')."""
    tokens = TOKEN_RE.findall(text)
    if len("".join(tokens)) < 30:
        return False
    good = sum(1 for t in tokens if WORDISH_RE.match(t) or NUMBER_RE.fullmatch(t.strip(".,;:")))
    return good / len(tokens) >= 0.5


# ---------- page text ----------
_text_cache: dict[str, list[str]] = {}
_text_lock = threading.Lock()


def page_texts(path: Path) -> list[str]:
    """Normalised text per page (PDF), per ~5k-character section (HTML) or per sheet (XLSX/CSV)."""
    key = file_hash(path)
    with _text_lock:
        if key in _text_cache:
            return _text_cache[key]
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        pages = []
        for page in PdfReader(str(path)).pages:
            try:
                pages.append(norm(page.extract_text() or ""))
            except Exception:
                pages.append("")
    elif suffix in (".htm", ".html"):
        from bs4 import BeautifulSoup

        text = BeautifulSoup(path.read_bytes(), "html.parser").get_text("\n")
        text = norm(re.sub(r"\n\s*\n+", "\n", text))
        pages = [text[i:i + HTML_SECTION] for i in range(0, len(text), HTML_SECTION)] or [""]
    elif suffix in (".xlsx", ".xlsm", ".csv"):
        from extract_financials import _sheet_text

        text = norm(_sheet_text(path))
        pages = [p for p in re.split(r"(?=^### Sheet: )", text, flags=re.M) if p.strip()] or [text]
    else:
        raise RuntimeError(f"Unsupported file type: {suffix}")
    with _text_lock:
        _text_cache[key] = pages
    return pages


# ---------- language / scanned detection ----------
EN_STOP = {"the", "and", "of", "to", "in", "for", "on", "with", "is", "by", "from", "as", "at", "was", "were", "are"}
LATIN_STOP = {
    "German": {"der", "die", "und", "des", "den", "mit", "für", "von", "im", "zum", "nicht"},
    "French": {"le", "la", "les", "et", "des", "du", "en", "pour", "sur", "une", "aux"},
    "Spanish": {"el", "los", "las", "y", "del", "en", "para", "con", "una", "por"},
    "Italian": {"il", "della", "delle", "e", "per", "con", "una", "degli", "nel"},
    "Portuguese": {"os", "das", "dos", "e", "para", "com", "uma", "pelo", "não"},
    "Dutch": {"het", "een", "van", "en", "voor", "met", "zijn", "niet"},
}


def doc_profile(pages: list[str]) -> dict:
    """{'scanned': bool, 'language': 'English' | other, 'image_pages': [i, ...]} from the extracted text."""
    n = max(len(pages), 1)
    image_pages = [i for i, p in enumerate(pages) if not readable(p)]
    scanned = len(image_pages) / n > 0.6
    text = " ".join(p for i, p in enumerate(pages) if i not in set(image_pages))
    letters = [ch for ch in text if ch.isalpha()]
    language = "English"
    if letters:
        latin = sum(1 for ch in letters if ch < "ɐ")
        if latin / len(letters) < 0.6:
            if any("぀" <= ch <= "ヿ" for ch in letters):
                language = "Japanese"
            elif any("가" <= ch <= "힯" for ch in letters):
                language = "Korean"
            elif any("一" <= ch <= "鿿" for ch in letters):
                language = "Chinese"
            elif any("Ѐ" <= ch <= "ӿ" for ch in letters):
                language = "Russian"
            elif any("؀" <= ch <= "ۿ" for ch in letters):
                language = "Arabic"
            else:
                language = "a language other than English"
        else:
            words = re.findall(r"[a-zà-öø-ÿ]+", text.lower())
            if len(words) > 200 and sum(1 for w in words if w in EN_STOP) / len(words) < 0.06:
                counts = {lang: sum(1 for w in words if w in stop) for lang, stop in LATIN_STOP.items()}
                best = max(counts, key=counts.get)
                language = best if counts[best] > 0.03 * len(words) else "a language other than English"
    return {"scanned": scanned, "language": language, "image_pages": image_pages}


# ---------- rule-based scoring ----------
def _statement_scores(text: str) -> dict:
    lines = text.splitlines()
    toc = _is_toc(lines)
    nums = numbers(text)
    s = soft(text)
    head, tail = fold("\n".join(lines[:6])), fold("\n".join(lines[-4:]))
    whole = fold(text)
    top = fold("\n".join(lines[:10]))
    # reports often hold several sets (consolidated and standalone / parent company): rank consolidated first
    scope = (2 if "consolidated" in top else 0) - (3 if re.search(
        r"standalone|separate(financial|statement|balance|income)|nonconsolidated|parentcompany", top) else 0)
    # summaries "extracted from" the statements (press releases) rank below the statements themselves
    scope -= 2 if "extractedfrom" in top else 0
    out = {}
    for st in STATEMENTS:
        edge = bool(TITLES[st].search(head) or TITLES[st].search(tail))
        anywhere = bool(TITLES[st].search(whole))
        anchors = sum(1 for a in ANCHORS[st] if a.search(s))
        qualifies = not toc and (edge or (anchors >= 3 and nums >= NUM_MIN))
        score = 4 * edge + 2 * anywhere + anchors + min(nums, 60) / 20 + scope
        out[st] = {"qualifies": qualifies, "score": score if not toc else 0, "anchors": anchors, "nums": nums,
                   "edge": edge}
    return out


def _cap_groups(groups: list[tuple[float, list[int]]], cap: int) -> list[int]:
    """Take page groups best-first until the cap; all of them when they fit."""
    keep: set[int] = set()
    allp = {p for _, g in groups for p in g}
    if len(allp) <= cap:
        return sorted(allp)
    for _, g in sorted(groups, key=lambda x: -x[0]):
        if len(keep | set(g)) > cap:
            continue
        keep |= set(g)
    return sorted(keep)


def rule_pages(path: Path) -> dict:
    """Pages per category found by the rules (0-based), plus 'weak': statements the rules could not find."""
    pages = page_texts(path)
    n = len(pages)
    scores = [_statement_scores(t) for t in pages]
    out, weak = {}, []
    st_groups = []
    for st in STATEMENTS:
        # pages titled as the statement come first; content-only pages (no title found) are the fallback
        titled = [i for i in range(n) if scores[i][st]["qualifies"] and scores[i][st]["edge"]]
        if any(scores[i][st]["anchors"] >= 2 for i in titled):
            found = titled
        else:
            content = sorted((i for i in range(n) if scores[i][st]["anchors"] >= 4
                              and scores[i][st]["nums"] >= NUM_MIN), key=lambda i: -scores[i][st]["score"])
            found = sorted(set(titled) | set(content[:2]))
            if not content:
                weak.append(st)
        groups = []
        for i in found:
            g = [i] + ([i + 1] if i + 1 < n else [])
            j = i + 2
            # long statements (banks, detailed cash flows) continue over more pages
            while j < n and j <= i + 3 and scores[j][st]["anchors"] >= 3 and scores[j][st]["nums"] >= NUM_MIN                     and not any(scores[j][o]["edge"] for o in STATEMENTS) and "notes to" not in soft(pages[j])[:300]:
                g.append(j)
                j += 1
            groups.append((scores[i][st]["score"], g))
        out[st] = sorted({p for _, g in groups for p in g})
        st_groups += groups
    out["statements"] = _cap_groups(st_groups, STATEMENT_PAGE_CAP)

    # non-GAAP / APM: keyword pages plus the next page; reconciliation tables first when there are too many
    ng = []
    for i, t in enumerate(pages):
        s = soft(t)
        strong, broad = len(NON_GAAP_STRONG_RE.findall(s)), len(NON_GAAP_BROAD_RE.findall(s))
        hits = strong + broad
        score = 10 * bool(RECONCIL_RE.search(s)) + hits + min(numbers(t), 100) / 25
        if strong:
            ng.append((score, [k for k in (i, i + 1) if k < n]))
        elif broad >= 2 and table_numbers(t) >= 15:
            ng.append((score, [i]))
    out["non_gaap"] = _cap_groups(ng, TABLE_PAGE_CAP)

    # segments: keyword pages that also look like a table, plus the next page; densest tables first
    sg = []
    for i, t in enumerate(pages):
        nums = table_numbers(t)
        s = soft(t)
        regions = {m.group(0).strip() for m in REGION_RE.finditer(s)}
        if (nums >= 8 and SEGMENT_RE.search(s)) or (
                nums >= SEGMENT_SOFT_MIN_NUMBERS and (SEGMENT_SOFT_RE.search(s) or len(regions) >= 2)):
            sg.append((nums, [k for k in (i, i + 1) if k < n]))
    out["segments"] = _cap_groups(sg, TABLE_PAGE_CAP)
    out["weak"] = weak
    return out


# ---------- Claude Haiku page finder ----------
AI_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        **{c: {"type": "array", "items": {"type": "integer"}} for c in CATEGORIES},
    },
    "required": ["language", *CATEGORIES],
    "additionalProperties": False,
}
AI_SYSTEM = """You locate tables in a company's financial report (any company, any country, any language). Return page numbers only.

Categories:
- income_statement: the primary income statement / statement of profit or loss / statement of operations (and a combined statement of comprehensive income).
- balance_sheet: the primary balance sheet / statement of financial position.
- cash_flow: the primary statement of cash flows.
- non_gaap: tables that reconcile reported (GAAP / IFRS) figures to adjusted, non-GAAP, non-IFRS or alternative performance measures (adjusted EBITDA, underlying profit, organic growth, free cash flow, net debt...), and tables of those measures.
- segments: tables that break revenue (or segment profit) down by business segment, product, geography or other dimension.
- guidance: pages with outlook, guidance, targets or other forward-looking statements by the company.

Rules: list every page of a table, including continuation pages. For the three statements use the primary financial statements (including condensed interim statements), not summaries in the management discussion, notes, or the table of contents. Use [] when a category is absent. language: the report's main language in English (e.g. "English", "German", "Chinese")."""
AI_TEXT_BUDGET = 600_000  # characters of page text sent to Haiku in one call
AI_PDF_PART_PAGES = 100
AI_PDF_PART_BYTES = 25_000_000

_ai_locks: dict[str, threading.Lock] = {}
_ai_locks_guard = threading.Lock()


def _ai_lock(key: str) -> threading.Lock:
    with _ai_locks_guard:
        return _ai_locks.setdefault(key, threading.Lock())


def pdf_subset(path: Path, pages: list[int]) -> bytes:
    """A PDF holding only the given pages (0-based), in order."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(path))
    writer = PdfWriter()
    for k in pages:
        if 0 <= k < len(reader.pages):
            writer.add_page(reader.pages[k])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _pdf_block(data: bytes) -> dict:
    import base64

    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                           "data": base64.standard_b64encode(data).decode()}}


def _ai_from_text(pages: list[str], client) -> tuple[dict, float]:
    per_page = max(600, min(3_000, AI_TEXT_BUDGET // max(len(pages), 1)))
    text = "\n".join(f"[Page {i + 1}]\n" + (t.strip()[:per_page] if readable(t) else "(no readable text)")
                     for i, t in enumerate(pages))
    content = [{"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text}},
               {"type": "text", "text": "List the pages for each category, using the [Page N] numbers. "
                                        "Each page shows only its first part."}]
    data, cost = call_json(client, content, AI_SCHEMA, effort="low", system=AI_SYSTEM, max_tokens=8000,
                           model=LOCATOR_MODEL)
    return {c: sorted({p - 1 for p in data[c] if 1 <= p <= len(pages)}) for c in CATEGORIES} | \
        {"language": data["language"]}, cost


def _ai_from_images(path: Path, page_idx: list[int], client) -> tuple[dict, float]:
    """Haiku reads the page images of the given pages (scanned PDFs, image-only slides), in parts."""
    out = {c: set() for c in CATEGORIES}
    language, cost = "", 0.0
    parts, cur = [], []
    for p in page_idx:
        cur.append(p)
        if len(cur) >= AI_PDF_PART_PAGES:
            parts.append(cur)
            cur = []
    if cur:
        parts.append(cur)
    for part in parts:
        data = pdf_subset(path, part)
        if len(data) > AI_PDF_PART_BYTES and len(part) > 1:  # very heavy scans: halve the part
            half = len(part) // 2
            for sub in (part[:half], part[half:]):
                r, c = _ai_from_images(path, sub, client)
                cost += c
                for k in CATEGORIES:
                    out[k] |= set(r[k])
                language = language or r["language"]
            continue
        content = [_pdf_block(data), {"type": "text", "text": (
            f"This PDF holds {len(part)} pages. Give page numbers by their position in this PDF "
            f"(1 = its first page), not the numbers printed on the pages.")}]
        r, c = call_json(client, content, AI_SCHEMA, effort="low", system=AI_SYSTEM, max_tokens=8000,
                         model=LOCATOR_MODEL)
        cost += c
        for k in CATEGORIES:
            out[k] |= {part[p - 1] for p in r[k] if 1 <= p <= len(part)}
        language = language or r["language"]
    return {k: sorted(v) for k, v in out.items()} | {"language": language}, cost


def ai_pages(path: Path, client, log=print) -> dict:
    """Pages per category found by Claude Haiku 5.5 (0-based). Cached per file; one call per file even when
    the four passes ask at the same time."""
    key = file_hash(path)
    cache = EXTRACT_DIR / f"{key}_pages_v{PAGES_VERSION}.json"
    with _ai_lock(key):
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            return {**data, "cost_usd": 0.0}
        if client is None:
            from common import get_client

            client = get_client()
        pages = page_texts(path)
        profile = doc_profile(pages)
        is_pdf = path.suffix.lower() == ".pdf"
        log(f"  {path.name}: AI page finder reading {'page images' if is_pdf and profile['scanned'] else 'page text'}")
        if is_pdf and profile["scanned"]:
            data, cost = _ai_from_images(path, list(range(len(pages))), client)
            mode = "images"
        else:
            data, cost = _ai_from_text(pages, client)
            mode = "text"
            # statements printed as images inside an otherwise text PDF (some slide decks)
            missing = [s for s in STATEMENTS if not data[s]]
            if is_pdf and missing and profile["image_pages"]:
                img, c = _ai_from_images(path, profile["image_pages"], client)
                cost += c
                for k in CATEGORIES:
                    data[k] = sorted(set(data[k]) | set(img[k]))
                mode = "text+images"
        data = {**data, "mode": mode, "cost": round(cost, 4)}
        EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, indent=1), encoding="utf-8")
        log(f"  {path.name}: AI page finder done (${cost:.3f})")
        return {**data, "cost_usd": cost}


# ---------- what the passes call ----------
def needs_ai(path: Path) -> tuple[bool, dict]:
    """Whether rules alone are not enough for this file: scanned, not English, or a statement not found."""
    pages = page_texts(path)
    profile = doc_profile(pages)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".csv"):
        return False, profile  # spreadsheets are sent whole
    rules = rule_pages(path)
    reason = ("scanned" if profile["scanned"] else
              f"in {profile['language']}" if profile["language"] != "English" else
              f"rules did not find: {', '.join(rules['weak'])}" if rules["weak"] else "")
    return bool(reason), {**profile, "reason": reason}


def find_pages(path: Path, category: str, client=None, need_ai: bool = False, log=print) -> dict:
    """Pages (0-based) to send for one category: 'statements', 'non_gaap', 'segments' or 'guidance'.

    Returns {'pages': [...], 'total': n, 'profile': {...}, 'ai': bool, 'cost_usd': float}.
    The AI page finder runs when rules are not enough (see needs_ai) or when need_ai is set (retry).
    'guidance' returns only AI pages (the guidance pass does its own text search when the file has text)."""
    pages = page_texts(path)
    n = len(pages)
    use_ai, profile = needs_ai(path)
    use_ai = use_ai or need_ai
    rules = rule_pages(path)
    ai, cost = None, 0.0
    if use_ai:
        ai = ai_pages(path, client, log)
        cost = ai.get("cost_usd", 0.0)
        if ai.get("language") and profile["language"] == "English" and ai["language"].lower() not in ("english", ""):
            profile["language"] = ai["language"]
    if category == "statements":
        rule = rules["statements"]
        extra = sorted({k for s in STATEMENTS for p in (ai or {}).get(s, []) for k in (p, p + 1) if k < n})
        cap = STATEMENT_PAGE_CAP
    elif category == "guidance":
        rule, extra, cap = [], (ai or {}).get("guidance", []), TABLE_PAGE_CAP
    else:
        rule = rules[category]
        extra = sorted({k for p in (ai or {}).get(category, []) for k in (p, p + 1) if k < n})
        cap = TABLE_PAGE_CAP
    chosen = list(extra)  # AI pages first, then rule pages, up to the cap
    for p in rule:
        if len(chosen) >= cap:
            break
        if p not in chosen:
            chosen.append(p)
    if len(extra) > cap:
        chosen = extra[:cap]
    return {"pages": sorted(chosen), "total": n, "profile": profile, "ai": ai is not None, "cost_usd": cost}


def has_candidates(path: Path, category: str) -> bool:
    """Free check: do the rules (or an earlier AI page finder run) see pages for this category?"""
    if rule_pages(path).get(category):
        return True
    cache = EXTRACT_DIR / f"{file_hash(path)}_pages_v{PAGES_VERSION}.json"
    if cache.exists():
        return bool(json.loads(cache.read_text(encoding="utf-8")).get(category))
    return False


HTML_TEXT_LIMIT = 400_000
MAX_FULL_PDF_PAGES = 120


def content_block(path: Path, category: str, client=None, need_ai: bool = False, log=print,
                  extra_pages=(), whole_if_none: bool = False) -> tuple[dict | None, dict]:
    """The Claude document block for one category, or None when the file has nothing for it.

    PDF: only the chosen pages. HTML: the whole text when short enough, else the chosen sections (+-1).
    Spreadsheets: the whole text (when the rules find something, or always for statements).
    whole_if_none: when no page is found, send a short PDF whole instead of nothing (statements)."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".csv"):
        info = {"pages": [], "total": 1, "profile": doc_profile(page_texts(path)), "ai": False, "cost_usd": 0.0}
        if not whole_if_none and not rule_pages(path).get(category):
            return None, info
        text = "\n".join(page_texts(path))
        return {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text}}, info
    info = find_pages(path, category, client, need_ai, log)
    pages = sorted(set(info["pages"]) | {p for p in extra_pages if 0 <= p < info["total"]})
    info["pages"] = pages
    if suffix == ".pdf":
        if not pages:
            if not whole_if_none:
                return None, info
            if info["total"] > MAX_FULL_PDF_PAGES:
                raise RuntimeError(f"Could not locate statement pages in a {info['total']}-page PDF. "
                                   f"Sending it whole would be expensive, so it was skipped.")
            info["pages"] = list(range(info["total"]))
            return _pdf_block(path.read_bytes()), info
        return _pdf_block(pdf_subset(path, pages)), info
    texts = page_texts(path)
    if not pages and not whole_if_none:
        return None, info
    if sum(len(t) for t in texts) <= HTML_TEXT_LIMIT:
        text = "\n".join(texts)
    else:
        keep = sorted({k for p in pages for k in (p - 1, p, p + 1) if 0 <= k < len(texts)}) or range(len(texts))
        parts, size = [], 0
        for k in keep:
            if size + len(texts[k]) > HTML_TEXT_LIMIT:
                break
            parts.append(texts[k])
            size += len(texts[k])
        text = "\n...\n".join(parts)
    return {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text}}, info


def language_note(profile: dict) -> str:
    lang = profile.get("language") or "English"
    if lang == "English":
        return ""
    return f" This report is in {lang}. Write every row label, column label, title and unit label in English."


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    f = Path(sys.argv[1])
    use_ai = "--ai" in sys.argv
    print(json.dumps(needs_ai(f)[1]))
    for cat in ("statements", "non_gaap", "segments", "guidance"):
        r = find_pages(f, cat, need_ai=use_ai) if use_ai else {"pages": rule_pages(f).get(cat, [])}
        print(cat, [p + 1 for p in r["pages"]])
