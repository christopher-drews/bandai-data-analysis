"""Apply ±5–15% random noise to sales numbers in the level_1 sales history.

Reads data/level_1_extract_sales_history/product_sales_history.csv and writes
anonymized output to data/level_2_anonymize_sales_history/. By default the
output is partitioned into one CSV per (start_month, Customer) — for
example ``2025-07_Heybox.csv``, ``2025-07_Sonkwo.csv`` — matching the
CLAUDE.md "one CSV per logical unit" convention. Pass ``--single-file`` to
keep the legacy single ``product_sales_history.csv`` output instead.

paxCode, Product Name, period, and currency are preserved verbatim — only
``amount`` and ``selling_price`` are perturbed.

Noise is per-row, per-column, independent: a uniform magnitude in [min_pct,
max_pct] with random sign. Use --seed for reproducible output.

After noising, every ``Customer == "All"`` row is split across the
SPLIT_TARGETS resellers (Heybox + Sonkwo) in the ratio of their assignable
post-noise sales. The split preserves the original row's total amount: the
first N-1 targets get ``round(amount * ratio, decimals)`` and the last
target gets the remainder. Zero/NaN-amount ``All`` rows are still split
(both targets get zero) so no ``*_All.csv`` file is ever written.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_INPUT = Path("data/level_1_extract_sales_history/product_sales_history.csv")
DEFAULT_OUTPUT_DIR = Path("data/level_2_anonymize_sales_history")
LEGACY_OUTPUT_NAME = "product_sales_history.csv"
NOISE_COLUMNS = ("amount", "selling_price")
SPLIT_SOURCE = "All"
SPLIT_TARGETS = ("Heybox", "Sonkwo")
SORT_COLUMNS = ["Product Name", "Customer", "start_month"]
PARTITION_COLUMNS = ("start_month", "Customer")


def apply_noise(series: pd.Series, rng: np.random.Generator, min_pct: float, max_pct: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    magnitude = rng.uniform(min_pct, max_pct, size=len(values))
    sign = rng.choice([-1.0, 1.0], size=len(values))
    factor = 1.0 + sign * magnitude
    return values * factor


def split_all_rows(df: pd.DataFrame, decimals: int) -> pd.DataFrame:
    """Replace every ``Customer == SPLIT_SOURCE`` row with one row per SPLIT_TARGETS reseller.

    Positive-amount rows are split in the ratio of each target's post-noise
    non-source sales totals. Zero/NaN-amount rows are still expanded so no
    SPLIT_SOURCE rows survive into the partitioned output — both targets
    simply receive a zero share.
    """
    if "Customer" not in df.columns:
        return df

    source_mask = df["Customer"] == SPLIT_SOURCE
    splittable = df[source_mask]
    if splittable.empty:
        return df

    target_totals = {
        name: float(df.loc[df["Customer"] == name, "amount"].fillna(0).clip(lower=0).sum())
        for name in SPLIT_TARGETS
    }
    total = sum(target_totals.values())
    if total > 0:
        ratios = {name: target_totals[name] / total for name in SPLIT_TARGETS}
    else:
        ratios = {name: 1.0 / len(SPLIT_TARGETS) for name in SPLIT_TARGETS}
        print(
            f"  warn: no assignable {SPLIT_TARGETS} sales — splitting {SPLIT_SOURCE!r} "
            f"rows evenly (only affects positive-amount rows; zero rows split to zero)",
            file=sys.stderr,
        )
    ratio_blurb = ", ".join(f"{name}={ratios[name]:.4f}" for name in SPLIT_TARGETS)
    print(f"  split ratio: {ratio_blurb}", file=sys.stderr)

    expanded_rows: list[dict] = []
    last_target = SPLIT_TARGETS[-1]
    for row in splittable.to_dict(orient="records"):
        raw_amount = row["amount"]
        amount = 0.0 if pd.isna(raw_amount) else float(raw_amount)
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

    out = pd.concat([df[~source_mask], pd.DataFrame(expanded_rows)], ignore_index=True)
    sort_cols = [c for c in SORT_COLUMNS if c in out.columns]
    if sort_cols:
        out = out.sort_values(sort_cols).reset_index(drop=True)
    print(
        f"  split {len(splittable)} {SPLIT_SOURCE!r} rows into "
        f"{len(expanded_rows)} {'/'.join(SPLIT_TARGETS)} rows",
        file=sys.stderr,
    )
    return out


def write_partitioned(df: pd.DataFrame, output_dir: Path) -> int:
    """Write one CSV per (start_month, Customer) group; clear stale CSVs first."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("*.csv"):
        stale.unlink()

    missing = [c for c in PARTITION_COLUMNS if c not in df.columns]
    if missing:
        print(f"Missing partition column(s) {missing}; cannot partition", file=sys.stderr)
        return 0

    written = 0
    for (month, customer), group in df.groupby(list(PARTITION_COLUMNS), dropna=False):
        if pd.isna(month) or pd.isna(customer):
            print(f"  warn: skipping group with NaN key: ({month!r}, {customer!r})", file=sys.stderr)
            continue
        month_str = str(month).strip()
        customer_str = str(customer).strip()
        if not month_str or not customer_str:
            print(f"  warn: skipping group with empty key: ({month_str!r}, {customer_str!r})", file=sys.stderr)
            continue
        path = output_dir / f"{month_str}_{customer_str}.csv"
        group.to_csv(path, index=False)
        written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT, type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path,
                        help="Directory to write per-(month, customer) CSVs into")
    parser.add_argument("--single-file", action="store_true",
                        help=f"Write one legacy CSV ({LEGACY_OUTPUT_NAME}) instead of per-(month, customer) files")
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

    if args.single_file:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        path = args.output_dir / LEGACY_OUTPUT_NAME
        df.to_csv(path, index=False)
        print(
            f"Wrote {path} ({len(df)} rows; noise ±{args.min_pct:.0%}–{args.max_pct:.0%})",
            file=sys.stderr,
        )
        return 0

    written = write_partitioned(df, args.output_dir)
    print(
        f"Wrote {written} per-(month, customer) CSVs to {args.output_dir} "
        f"({len(df)} rows total; noise ±{args.min_pct:.0%}–{args.max_pct:.0%})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
