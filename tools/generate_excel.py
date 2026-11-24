"""Merge per-report extractions into one Excel workbook: Income Statement, Balance Sheet, Cash Flow (+ Sources).

Usage:
    py tools/generate_excel.py <extractions.json> [--out path.xlsx]

Merge rules:
- A column is identified by (period_end, duration_months); when two reports cover the same column,
  the newest report wins (it reflects restatements).
- A row is identified by its normalized label plus occurrence number within the statement.
- Values are converted to the unit of the newest report (per-share/share-count rows are left as-is).
"""
import argparse
import json
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from common import OUTPUT_DIR, safe_filename

SHEETS = {"income_statement": "Income Statement", "balance_sheet": "Balance Sheet", "cash_flow": "Cash Flow"}


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def merge(extractions: list[dict]) -> dict:
    ordered = sorted(extractions, key=lambda e: e["source"].get("period_end") or "", reverse=True)
    merged = {}
    for stype in SHEETS:
        target_mult, unit_label = None, ""
        rows, row_order, cols, values = {}, [], {}, {}
        for ext in ordered:
            for st in (s for s in ext["statements"] if s["statement"] == stype):
                if target_mult is None:
                    target_mult, unit_label = st["unit_multiplier"], st["unit_label"]
                factor = st["unit_multiplier"] / target_mult
                col_keys = []
                for c in st["columns"]:
                    key = (c["period_end"], c["duration_months"])
                    cols.setdefault(key, c["label"])
                    col_keys.append(key)
                seen, prev = {}, None
                for r in st["rows"]:
                    base = _norm(r["label"])
                    seen[base] = seen.get(base, 0) + 1
                    rkey = f"{base}#{seen[base]}"
                    if rkey not in rows:
                        rows[rkey] = {"label": r["label"], "level": r["level"], "is_total": r["is_total"],
                                      "unscaled": r["unscaled"]}
                        pos = row_order.index(prev) + 1 if prev in row_order else len(row_order)
                        row_order.insert(pos, rkey)
                    prev = rkey
                    for ck, v in zip(col_keys, r["values"]):
                        if v is not None and (rkey, ck) not in values:
                            values[(rkey, ck)] = v if r["unscaled"] else v * factor
        col_order = sorted(cols, key=lambda k: (k[0], k[1]))
        merged[stype] = {
            "unit_label": unit_label,
            "columns": [{"key": k, "label": cols[k], "period_end": k[0], "duration_months": k[1]} for k in col_order],
            "rows": [{**rows[rk], "values": [values.get((rk, ck)) for ck in col_order]} for rk in row_order],
        }
    return merged


def _num_format(vals) -> str:
    decimals = 0
    for v in vals:
        if isinstance(v, float) and not v.is_integer():
            decimals = max(decimals, min(len(repr(v).split(".")[1]), 3))
    pat = "#,##0" + ("." + "0" * decimals if decimals else "")
    return f"{pat};({pat})"


def write_workbook(company: str, merged: dict, extractions: list[dict], out_path: Path,
                   non_gaap_merged: dict | None = None) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    bold, hdr_fill = Font(bold=True), PatternFill("solid", fgColor="DDE5F0")
    for stype, title in SHEETS.items():
        data = merged[stype]
        ws = wb.create_sheet(title)
        ws["A1"] = f"{company} - {title}"
        ws["A1"].font = Font(bold=True, size=13)
        ws["A2"] = f"Unit: {data['unit_label'] or 'n/a'} (per-share amounts and share counts as published)"
        ws.cell(4, 1, "Line item").font = bold
        ws.cell(5, 1, "Period end").font = bold
        for j, c in enumerate(data["columns"], start=2):
            ws.cell(4, j, c["label"]).font = bold
            ws.cell(5, j, c["period_end"])
            for r in (4, 5):
                ws.cell(r, j).fill = hdr_fill
                ws.cell(r, j).alignment = Alignment(horizontal="center")
            ws.column_dimensions[get_column_letter(j)].width = 15
        for r in (4, 5):
            ws.cell(r, 1).fill = hdr_fill
        for i, row in enumerate(data["rows"], start=6):
            cell = ws.cell(i, 1, row["label"])
            cell.alignment = Alignment(indent=min(row["level"], 6))
            if row["is_total"] or all(v is None for v in row["values"]):
                cell.font = bold
            fmt = "0.00##" if row["unscaled"] else _num_format(row["values"])
            for j, v in enumerate(row["values"], start=2):
                if v is not None:
                    c = ws.cell(i, j, v)
                    c.number_format = fmt
                    if row["is_total"]:
                        c.font = bold
        ws.column_dimensions["A"].width = 62
        ws.freeze_panes = "B6"
        if not data["rows"]:
            ws["A6"] = "This statement was not found in the downloaded reports."

    if non_gaap_merged is not None:
        from non_gaap_excel import write_non_gaap_sheets

        write_non_gaap_sheets(wb, company, non_gaap_merged)

    ws = wb.create_sheet("Sources")
    headers = ["Period", "Document type", "Period end", "Source URL", "File"]
    for j, h in enumerate(headers, start=1):
        ws.cell(1, j, h).font = bold
    for i, e in enumerate(sorted(extractions, key=lambda e: e["source"].get("period_end") or ""), start=2):
        s = e["source"]
        for j, v in enumerate([s.get("period_label"), s.get("doc_type"), s.get("period_end"), s.get("href"),
                               s.get("file_name") or Path(s.get("file") or "").name], start=1):
            ws.cell(i, j, v)
    for j, w in enumerate([14, 20, 14, 80, 50], start=1):
        ws.column_dimensions[get_column_letter(j)].width = w

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def build(company: str, extractions: list[dict], out_path: Path | None = None,
          non_gaap: list[dict] | None = None) -> tuple[Path, dict]:
    """non_gaap: results of extract_non_gaap.py; when given, the result dict also has merged['non_gaap']."""
    merged = merge(extractions)
    ng_merged = None
    if non_gaap is not None:
        from non_gaap_excel import merge_non_gaap

        ng_merged = merge_non_gaap(non_gaap)
    out_path = out_path or OUTPUT_DIR / f"{safe_filename(company)}_financials.xlsx"
    path = write_workbook(company, merged, extractions, out_path, ng_merged)
    return path, ({**merged, "non_gaap": ng_merged} if ng_merged is not None else merged)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("extractions_json")
    ap.add_argument("--out")
    ap.add_argument("--non-gaap", help="non_gaap.json from extract_non_gaap.py")
    args = ap.parse_args()
    meta = json.loads(Path(args.extractions_json).read_text(encoding="utf-8"))
    ng = json.loads(Path(args.non_gaap).read_text(encoding="utf-8"))["extractions"] if args.non_gaap else None
    path, merged = build(meta["company"], meta["extractions"], Path(args.out) if args.out else None, ng)
    merged.pop("non_gaap", None)
    print(json.dumps({"output": str(path), **{k: f"{len(v['rows'])} rows x {len(v['columns'])} periods"
                                               for k, v in merged.items()}}))
