"""Extract per-(product, reseller, promo) date windows from the level_0 CSVs.

Walks every CSV in data/level_0_export_royalty_csvs/, and for each
(product, reseller) per month reads the set of promo discounts (``Promo Discount
(OFF)`` > 0) **and** whether there were non-promo sales that month (a row with no
discount and Sales Units > 0). Product names are normalized and folded onto their
canonical SKU slug (merged spelling variants), then joined to skus_enriched.csv
for paxCode / Customer Reference.

Duration model (the report is monthly, so exact in-month dates are inferred)
---------------------------------------------------------------------------
Per (product, reseller), each month a discount appears is classified:

  * **full month** — that discount is the only promo that month AND there were no
    non-promo sales. The discount is taken to cover the whole month; consecutive
    full months of the same discount merge into one continuous span. This keeps
    genuine long-running discounts intact.
  * **partial** — non-promo sales coexisted, OR two or more discounts ran that
    month. Evidence that the promo did not fill the month, so it gets a short
    default window (``--default-days``, default 14). Multiple discounts in one
    month are **packed** back-to-back — each gets ``min(default_days,
    month_days / K)`` days for K discounts — so windows never overlap.

Because partial windows are packed within their month and full spans are
whole-month, the output has no overlapping ranges for a given (slug, reseller),
so LootVault's no-overlap rule is satisfied by construction.

Identical windows for both Heybox and Sonkwo (same slug, discount, dates) collapse
into one Customer=All row.

Discounts are **rounded to the nearest whole percent**. The report's Promo Discount
is a lossy ratio of integer prices (SRP vs selling price), so exact values are noisy
(e.g. 0.1852); nearest-1% keeps the implied promo price within ~1 CNY of the reported
selling price and merges near-identical discounts. Anything rounding to 0% is dropped.

Output: data/level_1_extract_promo_history/product_promo_history.csv with columns
    Product Name, Normalized Name, Customer, Promo Discount,
    paxCode, Customer Reference, start_date, end_date, basis
``basis`` records how the window was derived: ``full`` / ``partial`` / ``stacked``.
"""

from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from level_1_extract_srp_history import parse_period
from normalize import normalize_name
from pax_lookup import DEFAULT_PAX_CSV, build_pax_lookup, build_slug_alias_map

DEFAULT_INPUT_DIR = Path("data/level_0_export_royalty_csvs")
DEFAULT_OUTPUT = Path("data/level_1_extract_promo_history/product_promo_history.csv")

PROMO_COL_VARIANTS = ("Promo Discount\n(OFF)", "Promo Discount (OFF)")
AGGREGATE_CUSTOMERS = {"SUBTOTAL", "TOTAL"}
DEFAULT_PROMO_DAYS = 14

OUTPUT_COLUMNS = [
    "Product Name", "Normalized Name", "Customer", "Promo Discount",
    "paxCode", "Customer Reference", "start_date", "end_date", "basis",
]


def month_first(ym: str) -> date:
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, 1)


def month_last(ym: str) -> date:
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, calendar.monthrange(y, m)[1])


def extract_file_promo_state(path: Path) -> pd.DataFrame:
    """One row per (slug, customer, discount>0) with the month's promo context.

    Carries ``has_nonpromo`` (a non-promo sale existed that month) and
    ``n_discounts`` (distinct promo discounts that month) so the caller can tell
    full-month from partial without re-reading the file.
    """
    df = pd.read_csv(path)
    cols = {c.strip(): c for c in df.columns if isinstance(c, str)}
    pn = cols.get("Product Name")
    promo = next((cols[k] for k in PROMO_COL_VARIANTS if k in cols), None)
    cust = cols.get("Customer")
    units = cols.get("Sales Units")
    if not (pn and promo):
        return pd.DataFrame(columns=["Product Name", "Normalized Name", "Customer",
                                     "discount", "has_nonpromo", "n_discounts"])

    sub = pd.DataFrame({
        "Product Name": df[pn].astype(str).str.strip(),
        "Customer": (df[cust].astype(str).str.strip() if cust else "All"),
        "promo": pd.to_numeric(df[promo], errors="coerce").fillna(0.0),
        # No Sales Units column (shouldn't happen) -> treat every row as a sale.
        "units": (pd.to_numeric(df[units], errors="coerce").fillna(0) if units else 1),
    })
    sub["Customer"] = sub["Customer"].replace("", "All")
    sub = sub[sub["Product Name"].ne("") & sub["Product Name"].ne("nan")]
    sub = sub[~sub["Customer"].str.upper().isin(AGGREGATE_CUSTOMERS)]
    sub["Normalized Name"] = sub["Product Name"].map(normalize_name)
    sub = sub[sub["Normalized Name"] != ""]

    rows: list[dict] = []
    for (slug, customer), g in sub.groupby(["Normalized Name", "Customer"]):
        # The report's Promo Discount is a lossy ratio of integer prices, so exact
        # values are noisy (e.g. 0.1852). Round each to the nearest whole percent
        # (keeps the implied promo price within ~1 CNY); near-identical discounts
        # then merge, and anything rounding to 0% is dropped.
        discounts = sorted({
            r for d in g.loc[g["promo"] > 0, "promo"]
            if (r := round(float(d) * 100) / 100) > 0
        })
        if not discounts:
            continue
        has_nonpromo = bool(((g["promo"] == 0) & (g["units"] > 0)).any())
        pname = g["Product Name"].iloc[0]
        for d in discounts:
            rows.append({
                "Product Name": pname, "Normalized Name": slug, "Customer": customer,
                "discount": d, "has_nonpromo": has_nonpromo, "n_discounts": len(discounts),
            })
    return pd.DataFrame(rows)


def build_windows(per_period: pd.DataFrame, file_order: list[str], default_days: int) -> pd.DataFrame:
    """Turn per-period promo state into dated windows using the duration model."""
    file_idx = {s: i for i, s in enumerate(file_order)}
    per_period = per_period.copy()
    per_period["_idx"] = per_period["file"].map(file_idx)

    out: list[dict] = []
    for (slug, customer), g in per_period.groupby(["Normalized Name", "Customer"], sort=False):
        # Reconstruct each period's discount set + partial flag.
        periods: dict[int, dict] = {}
        for _, r in g.iterrows():
            p = periods.setdefault(int(r["_idx"]), {
                "sm": r["start_month"], "em": r["end_month"],
                "discounts": set(), "has_nonpromo": bool(r["has_nonpromo"]),
                "product": r["Product Name"],
            })
            p["discounts"].add(float(r["discount"]))

        full_runs: dict[float, list[int]] = {}  # discount -> sorted list of full-month period idxs
        for idx in sorted(periods):
            p = periods[idx]
            k = len(p["discounts"])
            partial = p["has_nonpromo"] or k >= 2
            if not partial:
                (d,) = tuple(p["discounts"])
                full_runs.setdefault(d, []).append(idx)
                continue
            # Partial: pack the k discounts sequentially within the period span.
            span_days = (month_last(p["em"]) - month_first(p["sm"])).days + 1
            length = min(default_days, span_days // k) if k else default_days
            length = max(length, 1)
            base = month_first(p["sm"])
            for i, d in enumerate(sorted(p["discounts"])):
                start = base + timedelta(days=i * length)
                end = start + timedelta(days=length - 1)
                out.append({
                    "Product Name": p["product"], "Normalized Name": slug, "Customer": customer,
                    "Promo Discount": d, "start_date": start, "end_date": end,
                    "basis": "stacked" if k >= 2 else "partial",
                })

        # Merge consecutive full-month periods (same discount) into one span.
        for d, idxs in full_runs.items():
            run_sm = run_em = None
            prev = None
            for idx in sorted(idxs):
                p = periods[idx]
                if prev is None:
                    run_sm, run_em, prev = p["sm"], p["em"], idx
                elif idx == prev + 1:
                    run_em, prev = p["em"], idx
                else:
                    out.append(_full_row(slug, customer, periods[prev]["product"], d, run_sm, run_em))
                    run_sm, run_em, prev = p["sm"], p["em"], idx
            if prev is not None:
                out.append(_full_row(slug, customer, periods[prev]["product"], d, run_sm, run_em))

    return pd.DataFrame(out)


def _full_row(slug, customer, product, discount, sm, em) -> dict:
    return {
        "Product Name": product, "Normalized Name": slug, "Customer": customer,
        "Promo Discount": discount, "start_date": month_first(sm), "end_date": month_last(em),
        "basis": "full",
    }


def collapse_heybox_sonkwo(runs: pd.DataFrame) -> pd.DataFrame:
    """Merge Heybox+Sonkwo rows sharing (slug, discount, dates) into one Customer=All row."""
    key = ["Normalized Name", "Promo Discount", "start_date", "end_date"]
    grp = runs.groupby(key, sort=False)["Customer"].agg(set).reset_index()
    pairs = grp[grp["Customer"].apply(lambda s: {"Heybox", "Sonkwo"}.issubset(s))][key]
    if pairs.empty:
        return runs
    pair_set = {tuple(r) for r in pairs.itertuples(index=False)}

    def is_pair(r: pd.Series) -> bool:
        return (r["Customer"] in {"Heybox", "Sonkwo"}
                and (r["Normalized Name"], r["Promo Discount"], r["start_date"], r["end_date"]) in pair_set)

    mask = runs.apply(is_pair, axis=1)
    merged = runs[mask].drop_duplicates(subset=key).copy()
    merged["Customer"] = "All"
    return pd.concat([runs[~mask], merged], ignore_index=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path)
    parser.add_argument("--pax-csv", default=DEFAULT_PAX_CSV, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    parser.add_argument("--default-days", default=DEFAULT_PROMO_DAYS, type=int,
                        help="Window length for a partial/stacked promo (default 14).")
    args = parser.parse_args()

    pax_lookup = build_pax_lookup(args.pax_csv)
    alias_map = build_slug_alias_map(args.pax_csv)
    print(f"Loaded {len(pax_lookup)} PAX-lookup entries, {len(alias_map)} variant aliases", file=sys.stderr)

    files = sorted(args.input_dir.glob("*.csv"), key=lambda p: parse_period(p)[0])
    file_order = [p.name for p in files]

    parts: list[pd.DataFrame] = []
    for path in files:
        start, end = parse_period(path)
        sub = extract_file_promo_state(path)
        if sub.empty:
            print(f"  skip {path.name!r}: no promo rows", file=sys.stderr)
            continue
        sub["Normalized Name"] = sub["Normalized Name"].map(lambda s: alias_map.get(s, s))
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
    runs = build_windows(per_period, file_order, args.default_days)
    runs = collapse_heybox_sonkwo(runs)

    # Canonical display name per slug: spelling from the most recent file.
    file_rank = {s: i for i, s in enumerate(file_order)}
    display_names = (
        per_period.assign(_rank=per_period["file"].map(file_rank))
        .sort_values("_rank")
        .drop_duplicates(subset=["Normalized Name"], keep="last")
        .set_index("Normalized Name")["Product Name"]
    )
    runs["Product Name"] = runs["Normalized Name"].map(display_names).fillna(runs["Product Name"])

    # Promo Discount as a fraction (e.g. 0.2); dates to ISO strings.
    runs["Promo Discount"] = runs["Promo Discount"].map(lambda x: round(float(x), 6))
    runs["start_date"] = runs["start_date"].map(lambda d: d.isoformat())
    runs["end_date"] = runs["end_date"].map(lambda d: d.isoformat())

    runs["paxCode"] = runs["Normalized Name"].map(lambda s: pax_lookup.get(s, ("", ""))[0])
    runs["Customer Reference"] = runs["Normalized Name"].map(lambda s: pax_lookup.get(s, ("", ""))[1])
    missing = sorted(set(runs.loc[~runs["Normalized Name"].isin(pax_lookup), "Product Name"]))

    runs = runs[OUTPUT_COLUMNS].sort_values(
        ["Product Name", "Customer", "start_date", "Promo Discount"]
    ).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    runs.to_csv(args.output, index=False)
    print(f"Wrote {args.output} ({len(runs)} promo windows across "
          f"{runs['Normalized Name'].nunique()} products)", file=sys.stderr)
    print("  basis:", runs["basis"].value_counts().to_dict(), file=sys.stderr)
    if missing:
        print(f"\n{len(missing)} product(s) had no PAX-lookup entry:", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
