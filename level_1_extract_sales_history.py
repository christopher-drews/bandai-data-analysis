"""Extract per-file sales rows from the level_0 CSVs and join PAX codes.

Walks every CSV in data/level_0_export_royalty_csvs/, pulls Product Name,
Customer, Sales Units, and Selling Price (CNY) for each row, normalizes
the product names via ``normalize.normalize_name``, joins to the
level_0_match_pax_codes output for ``paxCode``, and emits one row per
input row (no within-file aggregation — products can legitimately repeat
with different selling prices).

``Customer`` is the reseller name (Heybox / Sonkwo). Older files (before
2025-07) have no Customer column; those rows are labelled ``All``.
Aggregate rows (Customer = SUBTOTAL/TOTAL) are dropped.

Output: data/level_1_extract_sales_history/product_sales_history.csv with columns
    Product Name, Normalized Name, paxCode, Customer,
    amount, selling_price, currency, start_month, end_month
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from level_1_extract_srp_history import parse_period
from normalize import normalize_name

from pax_lookup import DEFAULT_PAX_CSV, build_pax_lookup, build_slug_alias_map

DEFAULT_INPUT_DIR = Path("data/level_0_export_royalty_csvs")
DEFAULT_OUTPUT = Path("data/level_1_extract_sales_history/product_sales_history.csv")
SALES_CURRENCY = "CNY"
AGGREGATE_CUSTOMERS = {"SUBTOTAL", "TOTAL"}

OUTPUT_COLUMNS = [
    "Product Name", "Normalized Name", "paxCode", "Customer",
    "amount", "selling_price", "currency", "start_month", "end_month",
]


def extract_file_sales(path: Path) -> pd.DataFrame:
    """Per-row Product Name + Customer + Sales Units + Selling Price (CNY) from one CSV."""
    df = pd.read_csv(path)
    cols = {c.strip(): c for c in df.columns if isinstance(c, str)}
    pn = cols.get("Product Name")
    units = cols.get("Sales Units")
    price = cols.get("Selling Price (CNY)")
    cust = cols.get("Customer")
    if not (pn and units and price):
        return pd.DataFrame(columns=["Product Name", "Normalized Name", "Customer", "amount", "selling_price"])

    keep = [pn, units, price] + ([cust] if cust else [])
    sub = df[keep].copy()
    rename = {pn: "Product Name", units: "amount", price: "selling_price"}
    if cust:
        rename[cust] = "Customer"
    sub = sub.rename(columns=rename)
    if "Customer" not in sub.columns:
        sub["Customer"] = "All"

    sub["Product Name"] = sub["Product Name"].astype(str).str.strip()
    sub["Customer"] = sub["Customer"].fillna("All").astype(str).str.strip()
    sub = sub[sub["Product Name"].ne("") & sub["Product Name"].ne("nan")]
    sub = sub[~sub["Customer"].str.upper().isin(AGGREGATE_CUSTOMERS)]
    sub["amount"] = pd.to_numeric(sub["amount"], errors="coerce")
    sub["selling_price"] = pd.to_numeric(sub["selling_price"], errors="coerce")
    sub = sub.dropna(subset=["amount", "selling_price"], how="all")
    sub["Normalized Name"] = sub["Product Name"].map(normalize_name)
    sub = sub[sub["Normalized Name"] != ""]
    return sub[["Product Name", "Normalized Name", "Customer", "amount", "selling_price"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path)
    parser.add_argument("--pax-csv", default=DEFAULT_PAX_CSV, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    pax_lookup = build_pax_lookup(args.pax_csv)
    alias_map = build_slug_alias_map(args.pax_csv)
    print(f"Loaded {len(pax_lookup)} PAX-lookup entries, {len(alias_map)} variant aliases", file=sys.stderr)

    files = sorted(args.input_dir.glob("*.csv"), key=lambda p: parse_period(p)[0])

    parts: list[pd.DataFrame] = []
    for path in files:
        start, end = parse_period(path)
        sub = extract_file_sales(path)
        if sub.empty:
            print(f"  skip {path.name!r}: no sales rows", file=sys.stderr)
            continue
        sub["start_month"] = start
        sub["end_month"] = end
        parts.append(sub)

    if not parts:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(args.output, index=False)
        print(f"Wrote {args.output} (0 rows)", file=sys.stderr)
        return 0

    rows = pd.concat(parts, ignore_index=True)
    # Fold merged spelling variants onto their canonical SKU slug (as SRP/promo do).
    rows["Normalized Name"] = rows["Normalized Name"].map(lambda s: alias_map.get(s, s))
    rows["currency"] = SALES_CURRENCY
    rows["paxCode"] = rows["Normalized Name"].map(lambda s: pax_lookup.get(s, ("", ""))[0])

    missing_mask = ~rows["Normalized Name"].isin(pax_lookup)
    missing = rows.loc[missing_mask, "Product Name"]

    rows = rows[OUTPUT_COLUMNS].sort_values(["Product Name", "Customer", "start_month"]).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows.to_csv(args.output, index=False)
    print(f"Wrote {args.output} ({len(rows)} rows)", file=sys.stderr)

    if not missing.empty:
        unique_missing = sorted(set(missing))
        print(f"\n{len(unique_missing)} product(s) had no PAX-lookup entry:", file=sys.stderr)
        for name in unique_missing:
            print(f"  - {name}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
