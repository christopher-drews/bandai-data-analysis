"""Extract per-month SRPs from the per-period level_0 CSVs and join PAX codes.

Walks every CSV in data/level_0_export_royalty_csvs/, pulls the ``SRP (CNY)``
column for each product, normalizes the product names via
``normalize.normalize_name``, joins to the level_0_match_pax_codes output
for ``paxCode`` and ``Customer Reference``, and collapses consecutive
same-SRP observations into start/end month ranges.

Output: data/level_1_extract_srp_history/product_srp_history.csv with columns
    Product Name, Normalized Name, SRP, currency, start_month, end_month,
    paxCode, Customer Reference
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

from normalize import normalize_name
from pax_lookup import DEFAULT_PAX_CSV, build_pax_lookup

DEFAULT_INPUT_DIR = Path("data/level_0_export_royalty_csvs")
DEFAULT_OUTPUT = Path("data/level_1_extract_srp_history/product_srp_history.csv")
SRP_CURRENCY = "CNY"

# Filenames are <YYYY-MM>.csv or <YYYY-MM>_<YYYY-MM>.csv.
FILENAME_RE = re.compile(r"^(\d{4}-\d{2})(?:_(\d{4}-\d{2}))?\.csv$")


def parse_period(path: Path) -> tuple[str, str]:
    m = FILENAME_RE.fullmatch(path.name)
    if not m:
        raise ValueError(f"Cannot parse period from filename: {path.name}")
    start = m.group(1)
    end = m.group(2) or start
    return start, end


def extract_file_srps(path: Path) -> pd.DataFrame:
    """Per-row Product Name + SRP (CNY) from one level_0 CSV, normalized."""
    df = pd.read_csv(path)
    cols = {c.strip(): c for c in df.columns if isinstance(c, str)}
    pn = cols.get("Product Name")
    srp = cols.get("SRP (CNY)")
    if not (pn and srp):
        return pd.DataFrame(columns=["Product Name", "Normalized Name", "SRP"])

    sub = df[[pn, srp]].rename(columns={pn: "Product Name", srp: "SRP"})
    sub["Product Name"] = sub["Product Name"].astype(str).str.strip()
    sub = sub[sub["Product Name"].ne("") & sub["Product Name"].ne("nan")]
    sub["SRP"] = pd.to_numeric(sub["SRP"], errors="coerce")
    sub = sub.dropna(subset=["SRP"])
    sub["Normalized Name"] = sub["Product Name"].map(normalize_name)
    sub = sub[sub["Normalized Name"] != ""]
    return sub[["Product Name", "Normalized Name", "SRP"]]


def collapse_runs(per_period: pd.DataFrame, file_order: list[str]) -> pd.DataFrame:
    """Collapse consecutive same-SRP files into (start_month, end_month) runs.

    Non-consecutive files (a gap in coverage) start a new run even if the SRP
    is unchanged.
    """
    file_idx = {s: i for i, s in enumerate(file_order)}
    per_period = per_period.copy()
    per_period["_idx"] = per_period["file"].map(file_idx)
    per_period = per_period.sort_values(["Normalized Name", "_idx"]).reset_index(drop=True)

    runs: list[dict] = []
    for slug, grp in per_period.groupby("Normalized Name", sort=False):
        grp = grp.sort_values("_idx")
        run_srp = None
        run_start_month = run_end_month = None
        prev_idx = None
        product_name = None
        for _, row in grp.iterrows():
            if run_srp is None:
                run_srp = row["SRP"]
                run_start_month = row["start_month"]
                run_end_month = row["end_month"]
                prev_idx = row["_idx"]
                product_name = row["Product Name"]
                continue
            if row["SRP"] == run_srp and row["_idx"] == prev_idx + 1:
                run_end_month = row["end_month"]
            else:
                runs.append({
                    "Product Name": product_name,
                    "Normalized Name": slug,
                    "SRP": run_srp,
                    "start_month": run_start_month,
                    "end_month": run_end_month,
                })
                run_srp = row["SRP"]
                run_start_month = row["start_month"]
                run_end_month = row["end_month"]
                product_name = row["Product Name"]
            prev_idx = row["_idx"]
        if run_srp is not None:
            runs.append({
                "Product Name": product_name,
                "Normalized Name": slug,
                "SRP": run_srp,
                "start_month": run_start_month,
                "end_month": run_end_month,
            })
    return pd.DataFrame(runs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path)
    parser.add_argument("--pax-csv", default=DEFAULT_PAX_CSV, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    pax_lookup = build_pax_lookup(args.pax_csv)
    print(f"Loaded {len(pax_lookup)} PAX-lookup entries", file=sys.stderr)

    files = sorted(args.input_dir.glob("*.csv"), key=lambda p: parse_period(p)[0])
    file_order = [p.name for p in files]

    parts: list[pd.DataFrame] = []
    for path in files:
        start, end = parse_period(path)
        sub = extract_file_srps(path)
        if sub.empty:
            print(f"  skip {path.name!r}: no SRP rows", file=sys.stderr)
            continue
        # If the same normalized product appears multiple times in one file,
        # keep the modal SRP and the first display name.
        sub = (
            sub.groupby("Normalized Name", as_index=False)
            .agg(**{
                "Product Name": ("Product Name", "first"),
                "SRP": ("SRP", lambda s: s.mode().iloc[0]),
            })
        )
        sub["file"] = path.name
        sub["start_month"] = start
        sub["end_month"] = end
        parts.append(sub)

    per_period = pd.concat(parts, ignore_index=True)
    runs = collapse_runs(per_period, file_order)

    # One canonical display name per slug — the spelling from the most recent
    # file that carried it.
    file_rank = {s: i for i, s in enumerate(file_order)}
    display_names = (
        per_period.assign(_rank=per_period["file"].map(file_rank))
        .sort_values("_rank")
        .drop_duplicates(subset=["Normalized Name"], keep="last")
        .set_index("Normalized Name")["Product Name"]
    )
    runs["Product Name"] = runs["Normalized Name"].map(display_names)

    runs["currency"] = SRP_CURRENCY
    runs["paxCode"] = ""
    runs["Customer Reference"] = ""
    missing: list[str] = []
    for i, slug in enumerate(runs["Normalized Name"]):
        entry = pax_lookup.get(slug)
        if entry is None:
            missing.append(runs.at[i, "Product Name"])
            continue
        runs.at[i, "paxCode"] = entry[0]
        runs.at[i, "Customer Reference"] = entry[1]

    runs = runs[[
        "Product Name", "Normalized Name", "SRP", "currency",
        "start_month", "end_month", "paxCode", "Customer Reference",
    ]].sort_values(["Product Name", "start_month"]).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    runs.to_csv(args.output, index=False)
    print(f"Wrote {args.output} ({len(runs)} rows)", file=sys.stderr)

    if missing:
        unique_missing = sorted(set(missing))
        print(f"\n{len(unique_missing)} product(s) had no PAX-lookup entry:", file=sys.stderr)
        for name in unique_missing:
            print(f"  - {name}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
