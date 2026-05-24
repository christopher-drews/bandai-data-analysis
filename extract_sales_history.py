"""Extract per-sheet sales rows from the Bandai royalty workbook and join PAX codes.

Walks every monthly sheet in BNEPA_Royalty_Report_*.xlsx, pulls Product Name,
Sales Units, and Selling Price (CNY) for each row, normalizes the product
names via ``normalize.normalize_name``, joins to ``royalty_pax_match.xlsx``
for the ``paxCode`` and ``Customer Reference`` columns, and emits one row per
workbook row (no within-sheet aggregation — products can legitimately repeat
in one sheet with different selling prices).

Output: product_sales_history.csv with columns
    Product Name, Normalized Name, paxCode, Customer Reference,
    amount, selling_price, currency, start_month, end_month
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import openpyxl
import pandas as pd

from extract_srp_history import (
    SKIP_SHEETS,
    build_pax_lookup,
    find_header_row,
    find_period,
    month_range,
)
from normalize import normalize_name

DEFAULT_WORKBOOK = Path("BNEPA_Royalty_Report_MAY2026_LV.xlsx")
DEFAULT_PAX_MATCH = Path("royalty_pax_match.xlsx")
DEFAULT_OUTPUT = Path("product_sales_history.csv")

SALES_CURRENCY = "CNY"

OUTPUT_COLUMNS = [
    "Product Name", "Normalized Name", "paxCode", "Customer Reference",
    "amount", "selling_price", "currency", "start_month", "end_month",
]


def extract_sheet_sales(path: Path, sheet: str) -> pd.DataFrame:
    """Per-row Product Name + Sales Units + Selling Price (CNY) from one sheet."""
    hdr = find_header_row(path, sheet)
    df = pd.read_excel(path, sheet_name=sheet, header=hdr, engine="openpyxl")
    cols = {c.strip(): c for c in df.columns if isinstance(c, str)}
    pn = cols.get("Product Name")
    units = cols.get("Sales Units")
    price = cols.get("Selling Price (CNY)")
    if not (pn and units and price):
        return pd.DataFrame(columns=["Product Name", "Normalized Name", "amount", "selling_price"])

    sub = df[[pn, units, price]].rename(columns={pn: "Product Name", units: "amount", price: "selling_price"})
    sub["Product Name"] = sub["Product Name"].astype(str).str.strip()
    sub = sub[sub["Product Name"].ne("") & sub["Product Name"].ne("nan")]
    sub["amount"] = pd.to_numeric(sub["amount"], errors="coerce")
    sub["selling_price"] = pd.to_numeric(sub["selling_price"], errors="coerce")
    sub = sub.dropna(subset=["amount", "selling_price"], how="all")
    sub["Normalized Name"] = sub["Product Name"].map(normalize_name)
    sub = sub[sub["Normalized Name"] != ""]
    return sub[["Product Name", "Normalized Name", "amount", "selling_price"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", default=DEFAULT_WORKBOOK, type=Path)
    parser.add_argument("--pax-match", default=DEFAULT_PAX_MATCH, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    pax_lookup = build_pax_lookup(args.pax_match)
    print(f"Loaded {len(pax_lookup)} PAX-match entries", file=sys.stderr)

    wb = openpyxl.load_workbook(args.workbook, read_only=False, data_only=True)
    monthly = [s for s in wb.sheetnames if s.strip() not in SKIP_SHEETS]

    sheet_periods = {}
    for sheet in monthly:
        period = find_period(wb[sheet])
        if period is None:
            print(f"  skip {sheet!r}: no sales period found", file=sys.stderr)
            continue
        sheet_periods[sheet] = period

    sheet_order = sorted(sheet_periods, key=lambda s: sheet_periods[s][0])

    parts: list[pd.DataFrame] = []
    for sheet in sheet_order:
        start, end = sheet_periods[sheet]
        months = month_range(start, end)
        sub = extract_sheet_sales(args.workbook, sheet)
        if sub.empty:
            print(f"  skip {sheet!r}: no sales rows", file=sys.stderr)
            continue
        sub["start_month"] = months[0]
        sub["end_month"] = months[-1]
        parts.append(sub)

    if not parts:
        pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(args.output, index=False)
        print(f"Wrote {args.output} (0 rows)", file=sys.stderr)
        return 0

    rows = pd.concat(parts, ignore_index=True)
    rows["currency"] = SALES_CURRENCY
    rows["paxCode"] = rows["Normalized Name"].map(lambda s: pax_lookup.get(s, ("", ""))[0])
    rows["Customer Reference"] = rows["Normalized Name"].map(lambda s: pax_lookup.get(s, ("", ""))[1])

    missing_mask = ~rows["Normalized Name"].isin(pax_lookup)
    missing = rows.loc[missing_mask, "Product Name"]

    rows = rows[OUTPUT_COLUMNS].sort_values(["Product Name", "start_month"]).reset_index(drop=True)
    rows.to_csv(args.output, index=False)
    print(f"Wrote {args.output} ({len(rows)} rows)", file=sys.stderr)

    if not missing.empty:
        unique_missing = sorted(set(missing))
        print(f"\n{len(unique_missing)} product(s) had no PAX-match entry:", file=sys.stderr)
        for name in unique_missing:
            print(f"  - {name}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
