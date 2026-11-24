"""Write the Guidance sheet from extract_guidance.py results: a log, one row per guidance item.

No merging: every statement from every report is kept, so reiterations and revisions of the same target
(e.g. an EV delivery target raised from 120k to 130k) sit next to each other after sorting by
category -> metric -> segment -> guided period -> issuing report.
"""
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from generate_excel import _norm

GUIDANCE_SHEET = "Guidance"
EMPTY_MSG = "No guidance was found in these reports."
CATEGORY_ORDER = {"financial": 0, "operational": 1, "esg": 2, "other": 3}
COLUMNS = [  # (header, row key, width)
    ("Category", "category", 12), ("Metric", "metric", 28), ("Segment", "segment", 18),
    ("Guided period", "guided_period", 16), ("Type", "type", 12), ("Low", "low", 12), ("High", "high", 12),
    ("Unit", "unit", 14), ("Basis", "basis", 16), ("Status", "status", 11),
    ("Issued in", "issued_in", 14), ("Report period end", "report_period_end", 14),
    ("Source file", "source_file", 30), ("Page", "page", 7), ("Quote", "quote", 90),
]


def guidance_rows(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        s = r.get("source", {})
        for it in r.get("items", []):
            rows.append({**{k: it.get(k) for k in ("category", "metric", "segment", "guided_period", "period_end",
                                                    "type", "low", "high", "unit", "basis", "status", "quote",
                                                    "page")},
                         "issued_in": s.get("period_label") or "",
                         "report_period_end": s.get("period_end") or "",
                         "source_file": s.get("file_name") or ""})
    rows.sort(key=lambda x: (CATEGORY_ORDER.get(x["category"], 9), _norm(x["metric"] or ""), _norm(x["segment"] or ""),
                             x["period_end"] or "9999", x["guided_period"] or "", x["report_period_end"]))
    return rows


def write_guidance_sheet(wb, company: str, rows: list[dict]) -> None:
    bold, hdr_fill = Font(bold=True), PatternFill("solid", fgColor="DDE5F0")
    ws = wb.create_sheet(GUIDANCE_SHEET)
    ws["A1"] = f"{company} - {GUIDANCE_SHEET}"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = ("Forward-looking statements as published, one row per statement and report. Values in the unit shown; "
                "rows of the same metric and period from different reports show how guidance changed.")
    for j, (title, _, width) in enumerate(COLUMNS, start=1):
        c = ws.cell(4, j, title)
        c.font, c.fill = bold, hdr_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = width
    for i, row in enumerate(rows, start=5):
        for j, (_, key, _) in enumerate(COLUMNS, start=1):
            v = row.get(key)
            c = ws.cell(i, j, v if v not in ("", None) else None)
            c.alignment = Alignment(vertical="top", wrap_text=key in ("quote", "metric", "source_file"))
            if key in ("low", "high") and isinstance(v, (int, float)):
                c.number_format = "#,##0.##" if isinstance(v, float) and not v.is_integer() else "#,##0"
    ws.freeze_panes = "C5"
    if rows:
        ws.auto_filter.ref = f"A4:{get_column_letter(len(COLUMNS))}{4 + len(rows)}"
    else:
        ws["A5"] = EMPTY_MSG
