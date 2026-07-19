"""Transform the level_1 sales history into per-SKU CSVs for `testdata add-sales`.

The lootvault CLI's ``testdata add-sales`` ingests a reseller's real historical
sales — one row per (product x month) with an exact quantity and per-unit price,
keyed by the SKU's ``customer_reference`` — and it stages/transfers any shortfall
and reports the sales itself (current API). This produces its input from our
level_1 sales history.

Output is one CSV per SKU, written under a per-reseller subdirectory so the
reseller keying add-sales needs is preserved:
``data/build_add_sales_csv/<reseller>/<customer_reference>.csv`` with the columns
add-sales expects:
    customer_reference, date (YYYY-MM), quantity, price, currency

One output row per level_1 row (no aggregation) — multiple price points in the
same month stay separate, which add-sales reports individually. Rows are kept
only when the SKU has a ``customer_reference`` (from the enriched SKU list);
Alibaba / blank-ref / non-positive rows are dropped. Prices are rounded to 2 dp
(add-sales rejects >2 dp).
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import pandas as pd

from pax_lookup import DEFAULT_PAX_CSV, build_pax_lookup

DEFAULT_SALES = Path("data/level_1_extract_sales_history/product_sales_history.csv")
DEFAULT_OUTPUT_DIR = Path("data/build_add_sales_csv")
RESELLERS = ("Heybox", "Sonkwo")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sales", default=DEFAULT_SALES, type=Path)
    parser.add_argument("--pax-csv", default=DEFAULT_PAX_CSV, type=Path,
                        help="Enriched SKU list supplying Normalized Name -> customer_reference")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
    args = parser.parse_args()

    # Normalized Name -> customer_reference (build_pax_lookup returns (paxCode, ref)).
    ref_by_slug = {slug: ref for slug, (_pax, ref) in build_pax_lookup(args.pax_csv).items()}

    df = pd.read_csv(args.sales, dtype=str, keep_default_na=False)
    df["qty"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0).astype(int)
    df["px"] = pd.to_numeric(df["selling_price"], errors="coerce")
    df["cref"] = df["Normalized Name"].map(ref_by_slug).fillna("")

    for reseller in RESELLERS:
        sub = df[(df["Customer"] == reseller) & (df["qty"] > 0) & (df["px"] > 0) & (df["cref"] != "")]
        reseller_dir = args.output_dir / reseller.lower()
        reseller_dir.mkdir(parents=True, exist_ok=True)
        for cref, rows in sub.groupby("cref"):
            path = reseller_dir / f"{cref}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["customer_reference", "date", "quantity", "price", "currency"])
                for _, r in rows.iterrows():
                    w.writerow([r["cref"], r["start_month"], r["qty"],
                                f"{round(float(r['px']), 2)}", r["currency"] or "CNY"])
        print(f"Wrote {sub['cref'].nunique()} SKU files to {reseller_dir}: "
              f"{len(sub)} rows, {int(sub['qty'].sum())} units", file=sys.stderr)

    dropped_ref = int(((df["Customer"].isin(RESELLERS)) & (df["qty"] > 0) & (df["cref"] == "")).sum())
    if dropped_ref:
        print(f"  ({dropped_ref} reseller rows dropped — SKU has no customer_reference)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
