"""Apply ±5–15% random noise to sales numbers in the level_1 sales history.

Reads data/level_1_extract_sales_history/product_sales_history.csv and writes
an anonymized copy to data/level_2_anonymize_sales_history/product_sales_history.csv.
Customer Reference, paxCode, Product Name, period, and currency are preserved
verbatim — only ``amount`` and ``selling_price`` are perturbed.

Noise is per-row, per-column, independent: a uniform magnitude in [min_pct,
max_pct] with random sign. Use --seed for reproducible output.
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


def apply_noise(series: pd.Series, rng: np.random.Generator, min_pct: float, max_pct: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    magnitude = rng.uniform(min_pct, max_pct, size=len(values))
    sign = rng.choice([-1.0, 1.0], size=len(values))
    factor = 1.0 + sign * magnitude
    return values * factor


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

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    print(
        f"Wrote {args.output} ({len(df)} rows; noise ±{args.min_pct:.0%}–{args.max_pct:.0%})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
