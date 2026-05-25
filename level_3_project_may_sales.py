"""Project partial-month sales for May 2026 (through 2026-05-26).

Reads data/level_2_anonymize_sales_history/product_sales_history.csv and
writes a projected May 2026 slice to
data/level_3_project_may_sales/product_sales_history.csv.

Method (hybrid seasonal × run-rate with carry-over fallback), per
(Normalized Name, paxCode, Customer, currency) group:

  ratio          = May2025_amount / Apr2025_amount   (if both > 0, else 1.0)
  projected      = Apr2026_amount × ratio × (26/31)
  noised         = projected × (1 ± uniform(min_pct, max_pct))
  selling_price  = Apr2026 selling_price, carried verbatim

Groups without an Apr 2026 anchor are skipped. Output rows use
start_month = end_month = "2026-05" and the same schema as the input.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_INPUT = Path("data/level_2_anonymize_sales_history/product_sales_history.csv")
DEFAULT_OUTPUT = Path("data/level_3_project_may_sales/product_sales_history.csv")

ANCHOR_MONTH = "2026-04"
TARGET_MONTH = "2026-05"
SEASON_NUM = "2025-05"
SEASON_DEN = "2025-04"
PRORATION = 26 / 31

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


def apply_noise(series: pd.Series, rng: np.random.Generator, min_pct: float, max_pct: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    magnitude = rng.uniform(min_pct, max_pct, size=len(values))
    sign = rng.choice([-1.0, 1.0], size=len(values))
    factor = 1.0 + sign * magnitude
    return values * factor


def month_slice(df: pd.DataFrame, month: str) -> pd.DataFrame:
    return df[(df["start_month"] == month) & (df["end_month"] == month)]


def aggregate_month(df: pd.DataFrame, month: str) -> pd.DataFrame:
    """Collapse multi-SKU price-point rows for a single month into one row per group.

    A given (product, customer, month) can legitimately appear on several rows
    representing different price tiers within the month. We sum amount and take
    an amount-weighted mean of selling_price so downstream math has one anchor
    per group.
    """
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
    parser.add_argument("--input", default=DEFAULT_INPUT, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    parser.add_argument("--min-pct", default=0.05, type=float, help="Minimum noise magnitude (default 0.05 = 5%%)")
    parser.add_argument("--max-pct", default=0.15, type=float, help="Maximum noise magnitude (default 0.15 = 15%%)")
    parser.add_argument("--seed", default=None, type=int, help="RNG seed for reproducibility")
    parser.add_argument("--decimals", default=2, type=int, help="Round noised values to this many decimals")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 1
    if args.min_pct < 0 or args.max_pct < args.min_pct:
        print("Invalid --min-pct/--max-pct range", file=sys.stderr)
        return 1

    df = pd.read_csv(args.input)
    single_month = df["start_month"] == df["end_month"]
    dropped = (~single_month).sum()
    if dropped:
        print(f"  dropping {dropped} multi-month rows (not single-month anchors)", file=sys.stderr)
    df = df[single_month]

    anchor = aggregate_month(df, ANCHOR_MONTH).set_index(GROUP_KEYS)
    season_num = aggregate_month(df, SEASON_NUM).set_index(GROUP_KEYS)["amount"]
    season_den = aggregate_month(df, SEASON_DEN).set_index(GROUP_KEYS)["amount"]

    if anchor.empty:
        print(f"No {ANCHOR_MONTH} anchor rows found in input; nothing to project", file=sys.stderr)
        return 1

    num = pd.to_numeric(season_num.reindex(anchor.index), errors="coerce")
    den = pd.to_numeric(season_den.reindex(anchor.index), errors="coerce")
    ratio = np.where((num > 0) & (den > 0), num / den, 1.0)
    fallback_count = int(((num.isna()) | (den.isna()) | (num <= 0) | (den <= 0)).sum())

    anchor_amount = pd.to_numeric(anchor["amount"], errors="coerce").fillna(0.0).to_numpy()
    projected = anchor_amount * ratio * PRORATION

    rng = np.random.default_rng(args.seed)
    noised = apply_noise(pd.Series(projected), rng, args.min_pct, args.max_pct).round(args.decimals)

    out = anchor.reset_index().copy()
    out["amount"] = noised.to_numpy()
    out["selling_price"] = pd.to_numeric(out["selling_price"], errors="coerce").round(args.decimals)
    out["start_month"] = TARGET_MONTH
    out["end_month"] = TARGET_MONTH
    out = out[OUTPUT_COLUMNS].sort_values(SORT_COLUMNS).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output, index=False)

    projected_count = len(out) - fallback_count
    print(
        f"Wrote {args.output} ({len(out)} rows; "
        f"{projected_count} with seasonal ratio, {fallback_count} carry-over fallback; "
        f"noise ±{args.min_pct:.0%}–{args.max_pct:.0%}; proration {PRORATION:.4f})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
