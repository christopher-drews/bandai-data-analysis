"""Apply ±5–15% random noise to sales numbers in the level_1 sales history.

Reads data/level_1_extract_sales_history/product_sales_history.csv and writes
an anonymized copy to data/level_2_anonymize_sales_history/product_sales_history.csv.
paxCode, Product Name, period, and currency are preserved verbatim — only
``amount`` and ``selling_price`` are perturbed.

Noise is per-row, per-column, independent: a uniform magnitude in [min_pct,
max_pct] with random sign. Use --seed for reproducible output.

After noising, rows with ``Customer == "All"`` and a positive ``amount`` are
split across the SPLIT_TARGETS resellers (Heybox + Sonkwo) in the ratio of
their assignable post-noise sales. The split preserves the original row's
total amount: the first N-1 targets get ``round(amount * ratio, decimals)``
and the last target gets the remainder. Zero/NaN-amount ``All`` rows are
left untouched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_INPUT = Path("data/level_1_extract_sales_history/product_sales_history.csv")
DEFAULT_OUTPUT = Path("data/level_2_anonymize_sales_history/product_sales_history.csv")
NOISE_COLUMNS = ("amount", "selling_price")
SPLIT_SOURCE = "All"
SPLIT_TARGETS = ("Heybox", "Sonkwo")
SORT_COLUMNS = ["Product Name", "Customer", "start_month"]


def apply_noise(series: pd.Series, rng: np.random.Generator, min_pct: float, max_pct: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    magnitude = rng.uniform(min_pct, max_pct, size=len(values))
    sign = rng.choice([-1.0, 1.0], size=len(values))
    factor = 1.0 + sign * magnitude
    return values * factor


def split_all_rows(df: pd.DataFrame, decimals: int) -> pd.DataFrame:
    """Replace ``Customer == SPLIT_SOURCE`` rows with one row per SPLIT_TARGETS reseller.

    Amounts are split in the ratio of each target's post-noise non-source
    sales totals. Rows whose amount is 0 / NaN are left as-is.
    """
    if "Customer" not in df.columns:
        return df

    target_totals = {
        name: float(df.loc[df["Customer"] == name, "amount"].fillna(0).clip(lower=0).sum())
        for name in SPLIT_TARGETS
    }
    total = sum(target_totals.values())
    if total <= 0:
        print(
            f"  warn: no assignable {SPLIT_TARGETS} sales — cannot split {SPLIT_SOURCE!r} rows",
            file=sys.stderr,
        )
        return df
    ratios = {name: target_totals[name] / total for name in SPLIT_TARGETS}
    ratio_blurb = ", ".join(f"{name}={ratios[name]:.4f}" for name in SPLIT_TARGETS)
    print(f"  split ratio: {ratio_blurb}", file=sys.stderr)

    source_mask = df["Customer"] == SPLIT_SOURCE
    splittable_mask = source_mask & df["amount"].notna() & (df["amount"] > 0)
    splittable = df[splittable_mask]
    if splittable.empty:
        return df

    expanded_rows: list[dict] = []
    last_target = SPLIT_TARGETS[-1]
    for row in splittable.to_dict(orient="records"):
        amount = float(row["amount"])
        remaining = amount
        for name in SPLIT_TARGETS:
            new_row = dict(row)
            new_row["Customer"] = name
            if name == last_target:
                share = round(remaining, decimals)
            else:
                share = round(amount * ratios[name], decimals)
                remaining -= share
            new_row["amount"] = share
            expanded_rows.append(new_row)

    out = pd.concat([df[~splittable_mask], pd.DataFrame(expanded_rows)], ignore_index=True)
    sort_cols = [c for c in SORT_COLUMNS if c in out.columns]
    if sort_cols:
        out = out.sort_values(sort_cols).reset_index(drop=True)
    print(
        f"  split {len(splittable)} {SPLIT_SOURCE!r} rows into "
        f"{len(expanded_rows)} {'/'.join(SPLIT_TARGETS)} rows",
        file=sys.stderr,
    )
    return out


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
    rng = np.random.default_rng(args.seed)

    for col in NOISE_COLUMNS:
        if col not in df.columns:
            print(f"Missing expected column {col!r}; skipping", file=sys.stderr)
            continue
        noised = apply_noise(df[col], rng, args.min_pct, args.max_pct)
        df[col] = noised.round(args.decimals)

    df = split_all_rows(df, args.decimals)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    print(
        f"Wrote {args.output} ({len(df)} rows; noise ±{args.min_pct:.0%}–{args.max_pct:.0%})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
