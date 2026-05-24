"""Extract per-(product, customer, promo) month ranges from the level_0 CSVs.

Walks every CSV in data/level_0_export_royalty_csvs/, pulls Product Name,
Customer, and Promo Discount (OFF) for each row, normalizes the product
names via ``normalize.normalize_name``, joins to the level_0_match_pax_codes
output for ``paxCode`` and ``Customer Reference``, and collapses consecutive
files with the same (product, customer, promo) into start/end month ranges.

Older files (before 2025-07) have no Customer column; those rows are
labelled ``All``. Aggregate rows (Customer = SUBTOTAL/TOTAL) and zero-promo
rows are dropped.

Output: data/level_1_extract_promo_history/product_promo_history.csv with columns
    Product Name, Normalized Name, Customer, Promo Discount,
    paxCode, Customer Reference, start_date, end_date

start_date/end_date are YYYY-MM-DD. By default they map to first-of-month /
last-of-month. When two consecutive runs for the same (slug, customer) share
a boundary month, that month is split: the earlier run ends on day 15, the
later run starts on day 16, so LootVault sees non-overlapping ranges.

Identical promotions (same slug, discount, dates) for both Heybox and Sonkwo
are collapsed into a single Customer=All row.
"""

from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from level_1_extract_srp_history import parse_period
from normalize import normalize_name
from pax_lookup import DEFAULT_PAX_CSV, build_pax_lookup

DEFAULT_INPUT_DIR = Path("data/level_0_export_royalty_csvs")
DEFAULT_OUTPUT = Path("data/level_1_extract_promo_history/product_promo_history.csv")

PROMO_COL_VARIANTS = ("Promo Discount\n(OFF)", "Promo Discount (OFF)")
AGGREGATE_CUSTOMERS = {"SUBTOTAL", "TOTAL"}

OUTPUT_COLUMNS = [
    "Product Name", "Normalized Name", "Customer", "Promo Discount",
    "paxCode", "Customer Reference", "start_date", "end_date",
]


def month_first(ym: str) -> date:
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, 1)


def month_last(ym: str) -> date:
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, calendar.monthrange(y, m)[1])


def collapse_heybox_sonkwo(runs: pd.DataFrame) -> pd.DataFrame:
    """Merge Heybox + Sonkwo rows with identical (slug, discount, dates) into one Customer=All row.

    A pair is only collapsed when no *other* row for the same slug (any customer,
    including a pre-existing All) overlaps the proposed date range. Otherwise the
    collapse would create an All-vs-reseller overlap that LootVault rejects.
    """
    key = ["Normalized Name", "Promo Discount", "start_month", "end_month"]
    grouped = (
        runs.groupby(key, sort=False)["Customer"].agg(set).reset_index()
    )
    candidates = grouped[
        grouped["Customer"].apply(lambda s: {"Heybox", "Sonkwo"}.issubset(s))
    ][key]
    if candidates.empty:
        return runs

    # Per-slug list of (start_date, end_date, customer, discount, start_month, end_month)
    # for every row, used to test whether a candidate collide with anything else.
    slug_rows: dict[str, list[tuple]] = {}
    for _, r in runs.iterrows():
        slug_rows.setdefault(r["Normalized Name"], []).append((
            month_first(r["start_month"]), month_last(r["end_month"]),
            r["Customer"], r["Promo Discount"],
            r["start_month"], r["end_month"],
        ))

    safe_pairs: set[tuple] = set()
    for _, c in candidates.iterrows():
        slug, disc, sm, em = c["Normalized Name"], c["Promo Discount"], c["start_month"], c["end_month"]
        c_start, c_end = month_first(sm), month_last(em)
        overlaps_other = False
        for r_start, r_end, r_cust, r_disc, r_sm, r_em in slug_rows[slug]:
            # Skip the candidate's own (Heybox, Sonkwo) rows.
            if (r_cust in {"Heybox", "Sonkwo"} and r_disc == disc
                    and r_sm == sm and r_em == em):
                continue
            if c_start <= r_end and r_start <= c_end:
                overlaps_other = True
                print(
                    f"  skip-collapse {slug} @ {disc} {sm}->{em}: would collide with "
                    f"{r_cust} {r_disc} {r_sm}->{r_em}",
                    file=sys.stderr,
                )
                break
        if not overlaps_other:
            safe_pairs.add((slug, disc, sm, em))

    if not safe_pairs:
        return runs

    def is_safe_pair_row(r: pd.Series) -> bool:
        return (
            r["Customer"] in {"Heybox", "Sonkwo"}
            and (
                r["Normalized Name"], r["Promo Discount"],
                r["start_month"], r["end_month"],
            ) in safe_pairs
        )

    is_pair = runs.apply(is_safe_pair_row, axis=1)
    pair_rows = runs[is_pair].drop_duplicates(subset=key).copy()
    pair_rows["Customer"] = "All"
    return pd.concat([runs[~is_pair], pair_rows], ignore_index=True)


def to_dates_with_conflict_split(runs: pd.DataFrame) -> pd.DataFrame:
    """Expand month columns to dates and split overlapping boundary months at 15/16."""
    runs = runs.copy()
    runs["start_date"] = runs["start_month"].map(month_first)
    runs["end_date"] = runs["end_month"].map(month_last)
    runs = runs.sort_values(
        ["Normalized Name", "Customer", "start_date"]
    ).reset_index(drop=True)

    for _, group_idx in runs.groupby(
        ["Normalized Name", "Customer"], sort=False
    ).groups.items():
        idx = list(group_idx)
        for a, b in zip(idx, idx[1:]):
            if runs.at[a, "end_date"] < runs.at[b, "start_date"]:
                continue
            conflict = runs.at[b, "start_date"]
            y, m = conflict.year, conflict.month
            mid, after = date(y, m, 15), date(y, m, 16)
            if runs.at[a, "start_date"] > mid or runs.at[b, "end_date"] < after:
                print(
                    f"  warn: complex conflict for "
                    f"{runs.at[a, 'Normalized Name']!r} / {runs.at[a, 'Customer']!r} "
                    f"at {conflict.isoformat()} — manual fix needed",
                    file=sys.stderr,
                )
                continue
            runs.at[a, "end_date"] = mid
            runs.at[b, "start_date"] = after

    runs["start_date"] = runs["start_date"].map(lambda d: d.isoformat())
    runs["end_date"] = runs["end_date"].map(lambda d: d.isoformat())
    return runs.drop(columns=["start_month", "end_month"])


def extract_file_promos(path: Path) -> pd.DataFrame:
    """Per-row Product Name + Customer + Promo Discount from one level_0 CSV."""
    df = pd.read_csv(path)
    cols = {c.strip(): c for c in df.columns if isinstance(c, str)}
    pn = cols.get("Product Name")
    promo = next((cols[k] for k in PROMO_COL_VARIANTS if k in cols), None)
    cust = cols.get("Customer")
    if not (pn and promo):
        return pd.DataFrame(columns=["Product Name", "Normalized Name", "Customer", "Promo Discount"])

    keep = [pn, promo] + ([cust] if cust else [])
    sub = df[keep].copy()
    rename = {pn: "Product Name", promo: "Promo Discount"}
    if cust:
        rename[cust] = "Customer"
    sub = sub.rename(columns=rename)
    if "Customer" not in sub.columns:
        sub["Customer"] = "All"

    sub["Product Name"] = sub["Product Name"].astype(str).str.strip()
    sub["Customer"] = sub["Customer"].fillna("All").astype(str).str.strip()
    sub = sub[sub["Product Name"].ne("") & sub["Product Name"].ne("nan")]
    sub = sub[~sub["Customer"].str.upper().isin(AGGREGATE_CUSTOMERS)]
    sub["Promo Discount"] = pd.to_numeric(sub["Promo Discount"], errors="coerce")
    sub = sub.dropna(subset=["Promo Discount"])
    sub = sub[sub["Promo Discount"] > 0]
    sub["Normalized Name"] = sub["Product Name"].map(normalize_name)
    sub = sub[sub["Normalized Name"] != ""]
    sub = sub.drop_duplicates(subset=["Normalized Name", "Customer", "Promo Discount"])
    return sub[["Product Name", "Normalized Name", "Customer", "Promo Discount"]]


def collapse_runs(per_period: pd.DataFrame, file_order: list[str]) -> pd.DataFrame:
    """Collapse consecutive same-(slug, customer, promo) files into runs.

    A gap in file coverage breaks the run.
    """
    file_idx = {s: i for i, s in enumerate(file_order)}
    per_period = per_period.copy()
    per_period["_idx"] = per_period["file"].map(file_idx)
    group_keys = ["Normalized Name", "Customer", "Promo Discount"]
    per_period = per_period.sort_values(group_keys + ["_idx"]).reset_index(drop=True)

    runs: list[dict] = []
    for (slug, customer, promo), grp in per_period.groupby(group_keys, sort=False):
        grp = grp.sort_values("_idx")
        run_start_month = run_end_month = None
        prev_idx = None
        product_name = None
        for _, row in grp.iterrows():
            if prev_idx is None:
                run_start_month = row["start_month"]
                run_end_month = row["end_month"]
                prev_idx = row["_idx"]
                product_name = row["Product Name"]
                continue
            if row["_idx"] == prev_idx + 1:
                run_end_month = row["end_month"]
            else:
                runs.append({
                    "Product Name": product_name,
                    "Normalized Name": slug,
                    "Customer": customer,
                    "Promo Discount": promo,
                    "start_month": run_start_month,
                    "end_month": run_end_month,
                })
                run_start_month = row["start_month"]
                run_end_month = row["end_month"]
                product_name = row["Product Name"]
            prev_idx = row["_idx"]
        if prev_idx is not None:
            runs.append({
                "Product Name": product_name,
                "Normalized Name": slug,
                "Customer": customer,
                "Promo Discount": promo,
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
        sub = extract_file_promos(path)
        if sub.empty:
            print(f"  skip {path.name!r}: no promo rows", file=sys.stderr)
            continue
        sub["file"] = path.name
        sub["start_month"] = start
        sub["end_month"] = end
        parts.append(sub)

    if not parts:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(args.output, index=False)
        print(f"Wrote {args.output} (0 rows)", file=sys.stderr)
        return 0

    per_period = pd.concat(parts, ignore_index=True)
    runs = collapse_runs(per_period, file_order)
    runs = collapse_heybox_sonkwo(runs)
    runs = to_dates_with_conflict_split(runs)

    # Canonical display name per slug: spelling from the most recent file.
    file_rank = {s: i for i, s in enumerate(file_order)}
    display_names = (
        per_period.assign(_rank=per_period["file"].map(file_rank))
        .sort_values("_rank")
        .drop_duplicates(subset=["Normalized Name"], keep="last")
        .set_index("Normalized Name")["Product Name"]
    )
    runs["Product Name"] = runs["Normalized Name"].map(display_names)

    runs["paxCode"] = runs["Normalized Name"].map(lambda s: pax_lookup.get(s, ("", ""))[0])
    runs["Customer Reference"] = runs["Normalized Name"].map(lambda s: pax_lookup.get(s, ("", ""))[1])

    missing_mask = ~runs["Normalized Name"].isin(pax_lookup)
    missing = runs.loc[missing_mask, "Product Name"]

    runs = runs[OUTPUT_COLUMNS].sort_values(
        ["Product Name", "Customer", "start_date", "Promo Discount"]
    ).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    runs.to_csv(args.output, index=False)
    print(
        f"Wrote {args.output} ({len(runs)} promo runs across "
        f"{runs['Normalized Name'].nunique()} products, "
        f"{runs[['Normalized Name', 'Customer']].drop_duplicates().shape[0]} "
        f"(product, customer) pairs)",
        file=sys.stderr,
    )

    if not missing.empty:
        unique_missing = sorted(set(missing))
        print(f"\n{len(unique_missing)} product(s) had no PAX-lookup entry:", file=sys.stderr)
        for name in unique_missing:
            print(f"  - {name}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
