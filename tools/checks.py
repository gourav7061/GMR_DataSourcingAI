"""Arithmetic checks on an extracted set of statements (any company): are the three statements there, and do
their totals add up? Used by extract_financials.py to decide on a retry, and written to the "Checks" sheet.

Each result: {statement, check, column, result: OK | FAIL | MISSING | n/a, detail}.
A check whose rows cannot be found is "n/a", never FAIL: layouts differ (banks have no gross profit, etc.).
"""
import re

STATEMENT_NAMES = {"income_statement": "Income statement", "balance_sheet": "Balance sheet", "cash_flow": "Cash flow"}


def _n(label: str) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()
    # drop section numbering such as "IV.", "(a)", "3.1"
    return re.sub(r"^((?:[ivx]{1,4}|[a-h]|\d+(?: \d+)*) )(?=[a-z])", "", t)


def _find(rows: list[dict], *patterns: str, last: bool = False, need_values: bool = True) -> dict | None:
    """First (or last) row whose normalised label fully matches one of the patterns."""
    hits = [r for r in rows if any(re.fullmatch(p, _n(r["label"])) for p in patterns)
            and (not need_values or any(v is not None for v in r["values"]))]
    if not hits:
        return None
    return hits[-1] if last else hits[0]


def _tol(*vals: float) -> float:
    big = max((abs(v) for v in vals if v is not None), default=0)
    return max(2.0, 0.005 * big)


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:,.2f}"


def _per_column(st: dict, name: str, fn) -> list[dict]:
    """Run fn(column_index) -> (result, detail) | None for each column with data."""
    out = []
    for j, col in enumerate(st["columns"]):
        r = fn(j)
        if r is None:
            continue
        out.append({"statement": STATEMENT_NAMES[st["statement"]], "check": name, "column": col["label"],
                    "result": r[0], "detail": r[1]})
    return out


def _v(row: dict | None, j: int) -> float | None:
    if row is None or j >= len(row["values"]):
        return None
    return row["values"][j]


def _balance_sheet(st: dict) -> list[dict]:
    rows = st["rows"]
    assets = _find(rows, r"total assets", r"assets total", r"total assets and liabilities")
    le = _find(rows, r"total liabilities (and|&) (shareholders |stockholders |owners )?(equity|deficit|capital)",
               r"total (shareholders |stockholders )?equity (and|&) liabilities",
               r"total liabilities (and|&) equity attributable.*", r"total equity and liabilities.*",
               r"liabilities and (shareholders |stockholders )?equity total")
    liab = _find(rows, r"total liabilities")
    eq = _find(rows, r"total (shareholders |stockholders )?equity", r"total equity( and reserves)?",
               r"total (shareholders |stockholders )?(equity|deficit)", r"total capital and reserves", last=True)

    def fn(j):
        a = _v(assets, j)
        if a is None:
            return None
        if le is not None and _v(le, j) is not None:
            other, how = _v(le, j), "liabilities + equity"
        elif _v(liab, j) is not None and _v(eq, j) is not None:
            other, how = _v(liab, j) + _v(eq, j), "total liabilities + total equity"
        else:
            return ("n/a", "Total liabilities / equity rows not found")
        ok = abs(a - other) <= _tol(a, other)
        return ("OK" if ok else "FAIL", f"Total assets {_fmt(a)} vs {how} {_fmt(other)}")

    return _per_column(st, "Assets = liabilities + equity", fn)


def _cash_flow(st: dict) -> list[dict]:
    rows = st["rows"]

    def act(word):
        cands = [r for r in rows if re.search(rf"\b{word} (activities|cash ?flows?)\b|cash ?flows? (from|used in) {word}\b",
                                              _n(r["label"])) and any(v is not None for v in r["values"])]
        totals = [r for r in cands if r["is_total"] or _n(r["label"]).startswith(("net", "cash", "total"))]
        return (totals or cands or [None])[-1]

    op, inv, fin = act("operating"), act("investing"), act("financing")
    # the effect of exchange rates on cash: the last exchange / currency row (it sits just before the net change)
    fx = _find(rows, r".*\b(exchange|currency|retranslations?|translation)\b.*", last=True)
    net = _find(rows, r"(net )?((increase|decrease|change)s? ){1,2}(in|of) .*cash.*",
                r"net cash (flow|flows)( for the (period|year))?", r"net (increase|decrease) .*", last=True)
    begin = _find(rows, r".*cash.*(at|as of)( the)? (beginning|start) of .*", r".*(beginning|opening) (cash|balance).*",
                  r"cash and cash equivalents at 1 .*")
    # closing cash: the first match after the opening row (later rows are often supplementary disclosures)
    after = rows[rows.index(begin) + 1:] if begin is not None else rows
    end = _find(after, r".*cash.*(at|as of)( the)? end of .*", r".*(ending|closing) (cash|balance).*")
    out = []

    def sum_fn(j):
        parts = [_v(op, j), _v(inv, j), _v(fin, j)]
        n = _v(net, j)
        if any(p is None for p in parts):
            return None if all(p is None for p in parts) else ("n/a", "An activities total is missing")
        if n is None:
            return ("n/a", "Net change in cash row not found")
        s = sum(parts)
        f = _v(fx, j) or 0
        if abs(s - n) <= _tol(s, n):
            return ("OK", f"Operating + investing + financing {_fmt(s)} vs net change {_fmt(n)}")
        ok = bool(f) and abs(s + f - n) <= _tol(s, n)
        return ("OK" if ok else "FAIL", f"Operating + investing + financing {_fmt(s)}"
                + (f" + FX effect {_fmt(f)}" if f else "") + f" vs net change {_fmt(n)}")

    def bridge_fn(j):
        b, n, e = _v(begin, j), _v(net, j), _v(end, j)
        if b is None or e is None:
            return None
        if n is None:
            return ("n/a", "Net change in cash row not found")
        f = _v(fx, j) or 0
        if abs(b + n - e) <= _tol(b, e):
            return ("OK", f"Opening {_fmt(b)} + change {_fmt(n)} vs closing {_fmt(e)}")
        ok = bool(f) and abs(b + n + f - e) <= _tol(b, e)
        return ("OK" if ok else "FAIL", f"Opening {_fmt(b)} + change {_fmt(n)}"
                + (f" + FX effect {_fmt(f)}" if f else "") + f" vs closing {_fmt(e)}")

    if op or inv or fin:
        out += _per_column(st, "Activities add up to net change in cash", sum_fn)
    out += _per_column(st, "Opening cash + change = closing cash", bridge_fn)
    return out


def _income_statement(st: dict) -> list[dict]:
    rows = st["rows"]
    rev = _find(rows, r"total (net )?(sales |operating )?revenues?", r"(net )?(sales |operating )?revenues?( from contracts with customers)?",
                r"(net )?sales", r"total (net )?sales", r"turnover", r"total income", r"total (net )?operating income",
                r"net interest income", r"revenues? and other income", r"(total )?net revenues? .*")
    cos = _find(rows, r"(total )?cost of (sales|revenues?|goods sold)( and services)?", r"total cost of revenues?",
                r"cost of sales and services")
    gp = _find(rows, r"gross (profit|margin)( loss)?", r"gross (loss|profit loss)")
    ni = _find(rows, r"net ((income|profit|earnings|loss) ){1,2}for the (period|year|quarter).*",
               r"((profit|loss|income|earnings) ){1,2}for the (period|year|quarter).*",
               r"net ((income|profit|earnings|loss) ?){1,2}",
               r"((profit|loss) ){1,2}after tax.*", r"net (income|profit|earnings|loss)( .*)?")
    out = []
    for name, row in (("Revenue line present", rev), ("Net profit line present", ni)):
        out.append({"statement": STATEMENT_NAMES["income_statement"], "check": name, "column": "",
                    "result": "OK" if row else "n/a", "detail": row["label"] if row else "No matching row found"})
    if rev and cos and gp:
        def fn(j):
            r, c, g = _v(rev, j), _v(cos, j), _v(gp, j)
            if r is None or c is None or g is None:
                return None
            calc = r - abs(c)
            ok = abs(calc - g) <= _tol(r, g)
            return ("OK" if ok else "FAIL", f"Revenue {_fmt(r)} - cost of sales {_fmt(abs(c))} = {_fmt(calc)} "
                                            f"vs gross profit {_fmt(g)}")
        out += _per_column(st, "Revenue - cost of sales = gross profit", fn)
    return out


def check_statements(data: dict) -> list[dict]:
    results = []
    by_type = {}
    for st in data.get("statements", []):
        by_type.setdefault(st["statement"], st)  # the first (primary) one of each type
    for stype, name in STATEMENT_NAMES.items():
        st = by_type.get(stype)
        has_values = st is not None and any(v is not None for r in st["rows"] for v in r["values"])
        results.append({"statement": name, "check": "Statement found", "column": "",
                        "result": "OK" if has_values else "MISSING",
                        "detail": (st["title"] if has_values else "Not found in the pages sent")})
        if not has_values:
            continue
        fn = {"income_statement": _income_statement, "balance_sheet": _balance_sheet, "cash_flow": _cash_flow}[stype]
        try:
            results += fn(st)
        except Exception as e:  # a check must never break an extraction
            results.append({"statement": name, "check": "Checks", "column": "", "result": "n/a", "detail": str(e)})
    return results


CHECKS_SHEET = "Checks"
COLUMNS = ["File", "Report period", "Statement", "Check", "Column", "Result", "Detail"]


def check_rows(extractions: list[dict]) -> list[dict]:
    """One row per check per file (oldest report first), plus a warning row when the files are from different
    companies. Extractions cached before checks existed get them computed here (free)."""
    rows = []
    for e in sorted(extractions, key=lambda e: e["source"].get("period_end") or ""):
        results = e.get("checks")
        if results is None:
            results = check_statements(e)
        s = e["source"]
        for r in results:
            rows.append({"File": s.get("file_name") or "", "Report period": s.get("period_label") or "",
                         "Statement": r["statement"], "Check": r["check"], "Column": r["column"],
                         "Result": r["result"], "Detail": r["detail"]})
    suffixes = r"\b(the|inc|incorporated|corporation|corp|co|company|ltd|limited|plc|se|sa|ag|nv|kk|group|holdings?)\b"
    names = {re.sub(r"\s+", " ", re.sub(suffixes, " ", re.sub(r"[^a-z0-9]+", " ", e["company"].lower()))).strip()
             for e in extractions if e.get("company")}
    if len(names) > 1:
        rows.insert(0, {"File": "", "Report period": "", "Statement": "All", "Check": "Same company in every file",
                        "Column": "", "Result": "FAIL",
                        "Detail": "Different companies detected: " + ", ".join(sorted(
                            {e["company"] for e in extractions if e.get("company")}))
                        + ". Their figures are merged into the same sheets."})
    return rows


def write_checks_sheet(wb, company: str, rows: list[dict]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(CHECKS_SHEET)
    ws["A1"] = f"{company} - Checks"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = ("Automatic checks on the extracted statements: present, and totals add up. "
                "FAIL or MISSING means verify that figure against the report. n/a means the rows needed were not found.")
    hdr_fill = PatternFill("solid", fgColor="DDE5F0")
    fills = {"OK": "E2F0D9", "FAIL": "F8D7DA", "MISSING": "F8D7DA", "n/a": "F2F2F2"}
    for j, h in enumerate(COLUMNS, start=1):
        c = ws.cell(4, j, h)
        c.font, c.fill = Font(bold=True), hdr_fill
    for i, r in enumerate(rows, start=5):
        for j, h in enumerate(COLUMNS, start=1):
            c = ws.cell(i, j, r[h])
            if h == "Result":
                c.fill = PatternFill("solid", fgColor=fills.get(r[h], "FFFFFF"))
                c.alignment = Alignment(horizontal="center")
    for j, w in enumerate([40, 16, 18, 40, 18, 10, 80], start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = "A5"
    if rows:
        ws.auto_filter.ref = f"A4:{get_column_letter(len(COLUMNS))}{4 + len(rows)}"
    else:
        ws["A5"] = "No statements were extracted, so there is nothing to check."


def summary(results: list[dict]) -> dict:
    return {"missing": sum(r["result"] == "MISSING" for r in results),
            "fail": sum(r["result"] == "FAIL" for r in results),
            "ok": sum(r["result"] == "OK" for r in results)}


def needs_retry(results: list[dict]) -> bool:
    s = summary(results)
    return bool(s["missing"] or s["fail"])


def better(a: list[dict], b: list[dict]) -> bool:
    """True when check results b are better than a (fewer missing statements, then fewer failures, more OKs)."""
    sa, sb = summary(a), summary(b)
    return (-sb["missing"], -sb["fail"], sb["ok"]) > (-sa["missing"], -sa["fail"], sa["ok"])
