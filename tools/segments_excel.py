"""Merge revenue-segment extractions (extract_segments.py) and write the Revenue Segments sheet.

Merge rules mirror generate_excel.merge():
- column = (period_end, duration_months); the newest report wins when two cover the same column (restatements)
- breakdown = dimension ("segment", "geography") or "other:<normalized name>"
- row = metric + normalized label + occurrence (not parent, so a line moved under a new segment stays one series;
  its nesting follows the newest report). Renamed lines stay separate rows, as published.
  Total lines are matched by metric alone and labelled "Total".
- values converted to the newest report's unit; gross margins (unscaled) left as published
"""
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from generate_excel import _norm, _num_format

SEGMENTS_SHEET = "Revenue Segments"
EMPTY_MSG = "No revenue breakdown was found in these reports."
METRICS = {"revenue": "Revenue", "gross_profit": "Gross profit", "gross_margin": "Gross margin (%)"}
DIM_ORDER = {"segment": 0, "geography": 1, "other": 2}


def _bkey(b: dict) -> str:
    return b["dimension"] if b["dimension"] != "other" else f"other:{_norm(b['name'])}"


def merge_segments(results: list[dict]) -> dict:
    ordered = sorted(results, key=lambda e: e["source"].get("period_end") or "", reverse=True)
    target_mult, unit_label = None, ""
    blocks, cols, values = {}, {}, {}
    for ext in ordered:
        for b in ext.get("breakdowns", []):
            if target_mult is None:
                target_mult, unit_label = b["unit_multiplier"], b["unit_label"]
            factor = b["unit_multiplier"] / target_mult
            bk = _bkey(b)
            blk = blocks.setdefault(bk, {"dimension": b["dimension"], "name": b["name"], "rows": {}, "order": []})
            col_keys = []
            for c in b["columns"]:
                key = (c["period_end"], c["duration_months"])
                cols.setdefault(key, c["label"])
                col_keys.append(key)
            seen, prev = {}, None
            for r in b["rows"]:
                # total lines are worded inconsistently ("Total", "Total revenue"), so match them by metric only.
                # Other lines match by label (not parent), so a line that moves under a new segment keeps one series.
                if r["is_total"]:
                    base = f"{r['metric']}|total"
                else:
                    base = f"{r['metric']}|{_norm(r['label'])}"
                seen[base] = seen.get(base, 0) + 1
                rkey = f"{bk}|{base}#{seen[base]}"
                if rkey not in blk["rows"]:
                    blk["rows"][rkey] = {k: r[k] for k in ("label", "metric", "parent", "level", "is_total", "unscaled")}
                    if r["is_total"]:
                        blk["rows"][rkey]["label"] = "Total"
                    pos = blk["order"].index(prev) + 1 if prev in blk["order"] else len(blk["order"])
                    blk["order"].insert(pos, rkey)
                prev = rkey
                for ck, v in zip(col_keys, r["values"]):
                    if v is not None and (rkey, ck) not in values:
                        values[(rkey, ck)] = v if r["unscaled"] else v * factor
    col_order = sorted(cols, key=lambda k: (k[0], k[1]))
    block_order = sorted(blocks, key=lambda k: DIM_ORDER[blocks[k]["dimension"]])  # stable: keeps first-seen order
    return {
        "unit_label": unit_label,
        "columns": [{"key": k, "label": cols[k], "period_end": k[0], "duration_months": k[1]} for k in col_order],
        "breakdowns": [
            {"dimension": blocks[bk]["dimension"], "name": blocks[bk]["name"],
             "rows": [{**blocks[bk]["rows"][rk], "values": [values.get((rk, ck)) for ck in col_order]}
                      for rk in blocks[bk]["order"]]}
            for bk in block_order
        ],
    }


def sheet_rows(merged: dict) -> list[dict]:
    """Rows for the sheet/app tab: dicts with label, level, bold, unscaled, values (None for headings)."""
    out = []
    for b in merged["breakdowns"]:
        out.append({"label": b["name"], "level": 0, "bold": True, "unscaled": False, "values": None})
        for metric, title in METRICS.items():
            rows = [r for r in b["rows"] if r["metric"] == metric]
            if not rows:
                continue
            out.append({"label": title, "level": 1, "bold": True, "unscaled": False, "values": None})
            out += [{"label": r["label"], "level": 2 + r["level"], "bold": r["is_total"], "unscaled": r["unscaled"],
                     "values": r["values"]} for r in rows]
        out.append({"label": "", "level": 0, "bold": False, "unscaled": False, "values": None})
    return out


def write_segments_sheet(wb, company: str, merged: dict) -> None:
    bold, hdr_fill = Font(bold=True), PatternFill("solid", fgColor="DDE5F0")
    ws = wb.create_sheet(SEGMENTS_SHEET)
    ws["A1"] = f"{company} - {SEGMENTS_SHEET}"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"Unit: {merged['unit_label'] or 'n/a'} (gross margins in %, as published)"
    ws.cell(4, 1, "Line item").font = bold
    ws.cell(5, 1, "Period end").font = bold
    for j, c in enumerate(merged["columns"], start=2):
        ws.cell(4, j, c["label"]).font = bold
        ws.cell(5, j, c["period_end"])
        for r in (4, 5):
            ws.cell(r, j).fill = hdr_fill
            ws.cell(r, j).alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(j)].width = 15
    for r in (4, 5):
        ws.cell(r, 1).fill = hdr_fill
    rows = sheet_rows(merged)
    for i, row in enumerate(rows, start=6):
        cell = ws.cell(i, 1, row["label"])
        cell.alignment = Alignment(indent=min(row["level"], 6))
        if row["bold"] or row["values"] is None:
            cell.font = bold
        if row["values"] is None:
            continue
        fmt = "0.0#" if row["unscaled"] else _num_format(row["values"])
        for j, v in enumerate(row["values"], start=2):
            if v is not None:
                c = ws.cell(i, j, v)
                c.number_format = fmt
                if row["bold"]:
                    c.font = bold
    ws.column_dimensions["A"].width = 62
    ws.freeze_panes = "B6"
    if not rows:
        ws["A6"] = EMPTY_MSG
