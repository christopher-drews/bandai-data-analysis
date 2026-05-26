"""Generate leftover/buffer inventory rows (5-10% of April 2026 keys per SKU per reseller).

Reads data/level_2_anonymize_sales_history/product_sales_history.csv and writes
data/level_3_left_over/product_sales_history.csv. Each output row represents
extra keys to be uploaded + transferred (but NOT reported as sold) for one
(paxCode, Customer) pair that had April 2026 sales.

Pipe the output through prepare_sales_upload.py (which does upload + transfer
only) and skip upload_sales_history.py — that gives the "upload, transfer,
no report" behavior.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_INPUT_DIR = Path("data/level_2_anonymize_sales_history")
DEFAULT_OUTPUT = Path("data/level_3_left_over/product_sales_history.csv")

ANCHOR_MONTH = "2026-04"
RESELLERS = ("Heybox", "Sonkwo")

GROUP_KEYS = ["Normalized Name", "paxCode", "Customer", "currency"]
OUTPUT_COLUMNS = [
    "Product Name",
    "Normalized Name",
    "paxCode",
    "Customer",
    "amount",
    "selling_price",
    "currency",
    "start_month",
    "end_month",
]
SORT_COLUMNS = ["Product Name", "Customer"]


def month_slice(df: pd.DataFrame, month: str) -> pd.DataFrame:
    return df[(df["start_month"] == month) & (df["end_month"] == month)]


def aggregate_month(df: pd.DataFrame, month: str) -> pd.DataFrame:
    sub = month_slice(df, month).copy()
    if sub.empty:
        return sub
    sub["amount"] = pd.to_numeric(sub["amount"], errors="coerce").fillna(0.0)
    sub["selling_price"] = pd.to_numeric(sub["selling_price"], errors="coerce")
    sub["_weighted_price"] = sub["selling_price"] * sub["amount"]
    agg = sub.groupby(GROUP_KEYS, dropna=False, as_index=False).agg(
        **{
            "Product Name": ("Product Name", "first"),
            "amount": ("amount", "sum"),
            "_weighted_price_sum": ("_weighted_price", "sum"),
            "_price_mean": ("selling_price", "mean"),
        }
    )
    agg["selling_price"] = np.where(
        agg["amount"] > 0,
        agg["_weighted_price_sum"] / agg["amount"].replace(0, np.nan),
        agg["_price_mean"],
    )
    return agg.drop(columns=["_weighted_price_sum", "_price_mean"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    parser.add_argument("--min-pct", default=0.05, type=float, help="Minimum leftover fraction (default 0.05 = 5%%)")
    parser.add_argument("--max-pct", default=0.10, type=float, help="Maximum leftover fraction (default 0.10 = 10%%)")
    parser.add_argument("--seed", default=None, type=int, help="RNG seed for reproducibility")
    args = parser.parse_args()

    if args.min_pct < 0 or args.max_pct < args.min_pct:
        print("Invalid --min-pct/--max-pct range", file=sys.stderr)
        return 1

    april_files = sorted(args.input_dir.glob(f"{ANCHOR_MONTH}_*.csv"))
    if not april_files:
        print(f"No {ANCHOR_MONTH}_*.csv files found in {args.input_dir}", file=sys.stderr)
        return 1

    df = pd.concat([pd.read_csv(p) for p in april_files], ignore_index=True)
    single_month = df["start_month"] == df["end_month"]
    df = df[single_month]

    anchor = aggregate_month(df, ANCHOR_MONTH)
    anchor = anchor[anchor["Customer"].isin(RESELLERS)].reset_index(drop=True)

    if anchor.empty:
        print(f"No {ANCHOR_MONTH} rows for {RESELLERS} in input", file=sys.stderr)
        return 1

    rng = np.random.default_rng(args.seed)
    april_amount = pd.to_numeric(anchor["amount"], errors="coerce").fillna(0.0).to_numpy()
    factor = rng.uniform(args.min_pct, args.max_pct, size=len(april_amount))
    leftover = np.array([math.ceil(a * f) for a, f in zip(april_amount, factor)], dtype=int)

    out = anchor.copy()
    out["amount"] = leftover
    out["selling_price"] = pd.to_numeric(out["selling_price"], errors="coerce").round(2)
    out["start_month"] = ANCHOR_MONTH
    out["end_month"] = ANCHOR_MONTH
    out = out[OUTPUT_COLUMNS].sort_values(SORT_COLUMNS).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output, index=False)

    print(
        f"Wrote {args.output} ({len(out)} rows, {int(out['amount'].sum())} total keys; "
        f"leftover {args.min_pct:.0%}-{args.max_pct:.0%} of {ANCHOR_MONTH} amounts)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
