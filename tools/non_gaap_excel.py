"""Merge non-GAAP extractions (extract_non_gaap.py) and write the GAAP Measures / Non-GAAP Measures sheets.

Merge rules mirror generate_excel.merge():
- column = (period_end, duration_months); the newest report wins when two cover the same column (restatements)
- row = normalized measure + normalized label + occurrence within that measure (GAAP and non-GAAP lines are
  matched by role + amount/ratio + occurrence instead, as their wording drifts between reports)
- values converted to the newest report's unit; per-share amounts, share counts and percentages left as published
"""
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from generate_excel import _norm, _num_format

NG_SHEETS = {"gaap": "GAAP Measures", "non_gaap": "Non-GAAP Measures"}
EMPTY_MSG = "No non-GAAP measures were found in these reports."


def merge_non_gaap(results: list[dict]) -> dict:
    ordered = sorted(results, key=lambda e: e["source"].get("period_end") or "", reverse=True)
    target_mult, unit_label, basis = None, "", ""
    measures, measure_order = {}, []  # mkey -> {"measure", "rows": {rkey: meta}, "order": [rkey]}
    cols, values = {}, {}
    for ext in ordered:
        basis = basis or ext.get("gaap_basis") or ""
        for rec in ext.get("reconciliations", []):
            if target_mult is None:
                target_mult, unit_label = rec["unit_multiplier"], rec["unit_label"]
            factor = rec["unit_multiplier"] / target_mult
            mkey = _norm(rec["measure"])
            if mkey not in measures:
                measures[mkey] = {"measure": rec["measure"], "rows": {}, "order": []}
                measure_order.append(mkey)
            m = measures[mkey]
            col_keys = []
            for c in rec["columns"]:
                key = (c["period_end"], c["duration_months"])
                cols.setdefault(key, c["label"])
                col_keys.append(key)
            seen, prev = {}, None
            for r in rec["rows"]:
                # GAAP / non-GAAP lines are matched by role and position, since wording drifts between
                # reports ("Profit for the period" vs "Profit for the period/year"); adjustments by label
                if r["role"] in ("gaap", "non_gaap"):
                    base = f"{r['role']}:{'ratio' if r['unscaled'] else 'amount'}"
                else:
                    base = _norm(r["label"])
                seen[base] = seen.get(base, 0) + 1
                rkey = f"{mkey}|{base}#{seen[base]}"
                if rkey not in m["rows"]:
                    m["rows"][rkey] = {k: r[k] for k in ("label", "role", "level", "unscaled")}
                    pos = m["order"].index(prev) + 1 if prev in m["order"] else len(m["order"])
                    m["order"].insert(pos, rkey)
                prev = rkey
                for ck, v in zip(col_keys, r["values"]):
                    if v is not None and (rkey, ck) not in values:
                        values[(rkey, ck)] = v if r["unscaled"] else v * factor
    col_order = sorted(cols, key=lambda k: (k[0], k[1]))
    return {
        "unit_label": unit_label,
        "gaap_basis": basis,
        "columns": [{"key": k, "label": cols[k], "period_end": k[0], "duration_months": k[1]} for k in col_order],
        "measures": [
            {"measure": measures[mk]["measure"],
             "rows": [{**measures[mk]["rows"][rk], "values": [values.get((rk, ck)) for ck in col_order]}
                      for rk in measures[mk]["order"]]}
            for mk in measure_order
        ],
    }


def sheet_rows(merged: dict, kind: str) -> list[dict]:
    """Rows for one sheet: dicts with label, level, bold, unscaled, values (values None for headings)."""
    out = []
    if kind == "gaap":
        for m in merged["measures"]:
            for r in m["rows"]:
                if r["role"] == "gaap":
                    out.append({"label": r["label"], "level": 0, "bold": False, "unscaled": r["unscaled"],
                                "values": r["values"]})
        return out
    summary = [r for m in merged["measures"] for r in m["rows"] if r["role"] == "non_gaap"]
    if summary:
        out.append({"label": "Summary", "level": 0, "bold": True, "unscaled": False, "values": None})
        out += [{"label": r["label"], "level": 1, "bold": False, "unscaled": r["unscaled"], "values": r["values"]}
                for r in summary]
        out.append({"label": "", "level": 0, "bold": False, "unscaled": False, "values": None})
        out.append({"label": "Reconciliations", "level": 0, "bold": True, "unscaled": False, "values": None})
    for m in merged["measures"]:
        out.append({"label": m["measure"], "level": 0, "bold": True, "unscaled": False, "values": None})
        for r in m["rows"]:
            level = 2 if r["role"] == "adjustment" else 1
            out.append({"label": r["label"], "level": max(level, r["level"] + 1), "bold": r["role"] == "non_gaap",
                        "unscaled": r["unscaled"], "values": r["values"]})
        out.append({"label": "", "level": 0, "bold": False, "unscaled": False, "values": None})
    return out


def write_non_gaap_sheets(wb, company: str, merged: dict) -> None:
    bold, hdr_fill = Font(bold=True), PatternFill("solid", fgColor="DDE5F0")
    basis = merged.get("gaap_basis") or "GAAP"
    for kind, title in NG_SHEETS.items():
        ws = wb.create_sheet(title)
        ws["A1"] = f"{company} - {title}"
        ws["A1"].font = Font(bold=True, size=13)
        note = (f"Measures as reported under {basis} that the company reconciles to non-GAAP measures"
                if kind == "gaap" else f"Non-GAAP measures and their reconciliation from {basis}")
        ws["A2"] = (f"Unit: {merged['unit_label'] or 'n/a'} (per-share amounts and percentages as published). "
                    f"{note}.")
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
        rows = sheet_rows(merged, kind)
        for i, row in enumerate(rows, start=6):
            cell = ws.cell(i, 1, row["label"])
            cell.alignment = Alignment(indent=min(row["level"], 6))
            if row["bold"] or row["values"] is None:
                cell.font = bold
            if row["values"] is None:
                continue
            fmt = "0.00##" if row["unscaled"] else _num_format(row["values"])
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
