"""Extract per-(product, customer, promo) month ranges from the royalty workbook.

Walks every monthly sheet in BNEPA_Royalty_Report_*.xlsx, pulls Product Name,
Customer, and Promo Discount (OFF) for each row, normalizes the product names
via ``normalize.normalize_name``, joins to ``royalty_pax_match.xlsx`` for
``paxCode`` (PAX column only — no fallback) and ``Customer Reference``, and
collapses consecutive sheets with the same (product, customer, promo) into
start/end month ranges.

Older sheets (8月9月 → 6月) have no Customer column; those rows are labelled
``All``. Aggregate rows (Customer = SUBTOTAL/TOTAL) and zero-promo rows are
dropped.

Output: product_promo_history.csv with columns
    Product Name, Normalized Name, Customer, Promo Discount,
    paxCode, Customer Reference, start_month, end_month
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import openpyxl
import pandas as pd

from extract_srp_history import (
    SKIP_SHEETS,
    find_header_row,
    find_period,
    month_range,
)
from normalize import normalize_name

DEFAULT_WORKBOOK = Path("BNEPA_Royalty_Report_MAY2026_LV.xlsx")
DEFAULT_PAX_MATCH = Path("royalty_pax_match.xlsx")
DEFAULT_OUTPUT = Path("product_promo_history.csv")

PROMO_COL_VARIANTS = ("Promo Discount\n(OFF)", "Promo Discount (OFF)")
AGGREGATE_CUSTOMERS = {"SUBTOTAL", "TOTAL"}

OUTPUT_COLUMNS = [
    "Product Name", "Normalized Name", "Customer", "Promo Discount",
    "paxCode", "Customer Reference", "start_month", "end_month",
]


def build_pax_lookup_no_fallback(path: Path) -> dict[str, tuple[str, str]]:
    """normalize_name(Name) -> (paxCode, customer_reference) using PAX only.

    Rows where PAX is NaN are skipped — no fallback_pax_code substitution.
    """
    df = pd.read_excel(path, sheet_name="SKUs", engine="openpyxl")
    df = df[["Name", "PAX", "Customer Reference"]].copy()
    df = df[df["PAX"].notna()]
    df["slug"] = df["Name"].map(normalize_name)
    df = df[df["slug"] != ""]

    lookup: dict[str, tuple[str, str]] = {}
    for slug, group in df.groupby("slug"):
        if len(group) > 1:
            print(f"  warn: duplicate slug {slug!r} in pax-match ({len(group)} rows)", file=sys.stderr)
        row = group.iloc[0]
        pax = row["PAX"]
        ref = row["Customer Reference"] if pd.notna(row["Customer Reference"]) else ""
        lookup[slug] = (pax, ref)
    return lookup


def extract_sheet_promos(path: Path, sheet: str) -> pd.DataFrame:
    """Per-row Product Name + Customer + Promo Discount from one sheet."""
    hdr = find_header_row(path, sheet)
    df = pd.read_excel(path, sheet_name=sheet, header=hdr, engine="openpyxl")
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


def collapse_runs(per_month: pd.DataFrame, sheet_order: list[str]) -> pd.DataFrame:
    """Collapse consecutive same-(slug, customer, promo) sheets into runs.

    A gap in sheet coverage breaks the run.
    """
    sheet_idx = {s: i for i, s in enumerate(sheet_order)}
    per_month = per_month.copy()
    per_month["_idx"] = per_month["sheet"].map(sheet_idx)
    group_keys = ["Normalized Name", "Customer", "Promo Discount"]
    per_month = per_month.sort_values(group_keys + ["_idx"]).reset_index(drop=True)

    runs: list[dict] = []
    for (slug, customer, promo), grp in per_month.groupby(group_keys, sort=False):
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
    parser.add_argument("--workbook", default=DEFAULT_WORKBOOK, type=Path)
    parser.add_argument("--pax-match", default=DEFAULT_PAX_MATCH, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    pax_lookup = build_pax_lookup_no_fallback(args.pax_match)
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
        sub = extract_sheet_promos(args.workbook, sheet)
        if sub.empty:
            print(f"  skip {sheet!r}: no promo rows", file=sys.stderr)
            continue
        sub["sheet"] = sheet
        sub["start_month"] = months[0]
        sub["end_month"] = months[-1]
        parts.append(sub)

    if not parts:
        pd.DataFrame(columns=OUTPUT_COLUMNS).to_csv(args.output, index=False)
        print(f"Wrote {args.output} (0 rows)", file=sys.stderr)
        return 0

    per_month = pd.concat(parts, ignore_index=True)
    runs = collapse_runs(per_month, sheet_order)

    # Canonical display name per slug: spelling from the most recent sheet.
    sheet_rank = {s: i for i, s in enumerate(sheet_order)}
    display_names = (
        per_month.assign(_rank=per_month["sheet"].map(sheet_rank))
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
        ["Product Name", "Customer", "start_month", "Promo Discount"]
    ).reset_index(drop=True)
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
        print(f"\n{len(unique_missing)} product(s) had no PAX-match entry:", file=sys.stderr)
        for name in unique_missing:
            print(f"  - {name}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
