"""Identify the unique Bandai SKUs from the per-period royalty CSVs (offline).

Part 1 of the two-step SKU pipeline (Part 2 = level_2_enrich_pax_codes.py, the
API leg). This step reads only data/level_0_export_royalty_csvs/*.csv — no
network, fully deterministic — and emits one row per unique SKU keyed on the
**normalized Product Name**, each carrying its **Customer Reference** (the report's
``Item Number``) chosen by temporal dominance.

Item Number families
--------------------
Valid codes match ``^[EL]\\d{5}$`` — the ``E#####`` (established) and ``L#####``
(newer, from 2025-07) families are both legitimate. Anything else (notably the
literal ``0`` placeholder seen in some sheets) is treated as malformed and dropped.

Corruption handling: auto-fix by dominance, then flag
-----------------------------------------------------
The report's Item Number is mostly stable per product, with occasional transient
single-month errors (a wrong code in one sheet, or a code copied from another
game). For each normalized product we count the number of **distinct periods**
each valid Item Number appears in and pick the one with the widest coverage as
canonical. This auto-corrects the common "20 months of E05471, 1 month of a wrong
E03730" case.

Known spelling variants
-----------------------
When two products carry the **same canonical Item Number** they are the same SKU
spelled differently (e.g. ``GOD EATER 3`` vs ``God Eaters 3``, both E03177). These
are merged only when the code is listed in the curated known-variants file
(``--known-variants``, default data/known_name_variants.csv: ``item_number,
correct_name, note``) — that file is the control surface, so a merge is always a
deliberate, reviewable act. The merged SKU takes the curated ``correct_name``.
A shared code **not** in the list is left as a ``shared_item_number`` flag for a
human to resolve (confirm it's a variant and add it, or investigate a real reuse).

Whatever the dominance rule cannot resolve cleanly is written to a review file
rather than silently decided:
  - competing_item_numbers — a runner-up Item Number with non-trivial coverage
    (a genuine tie / re-code, not a 1-month blip)
  - shared_item_number — the canonical code is claimed by >1 product and is NOT
    in the known-variants list (un-merged variant, or true cross-game reuse)
  - merged_variant — informational: which spellings were merged via the list
  - no_valid_item_number — every observation was malformed (canonical left blank)
  - malformed_dropped — informational: which bad values were discarded

Outputs (data/level_1_extract_skus/):
  - skus.csv    one row per unique SKU; columns:
      Product Name, Normalized Name, Customer Reference, months_seen,
      first_period, last_period, resellers, item_number_candidates,
      needs_review, review_reasons
  - review.csv  one row per flagged issue; columns:
      Normalized Name, Product Name, issue_type, detail
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from normalize import normalize_name

DEFAULT_INPUT_DIR = Path("data/level_0_export_royalty_csvs")
DEFAULT_OUTPUT_DIR = Path("data/level_1_extract_skus")
DEFAULT_KNOWN_VARIANTS = Path("data/known_name_variants.csv")

VALID_ITEM_NUMBER = re.compile(r"^[EL]\d{5}$")
AGGREGATE_CUSTOMERS = {"SUBTOTAL", "TOTAL"}

# A runner-up Item Number is only "competing" (worth manual review) when it covers
# at least this many distinct periods. Transient single-period blips (coverage 1)
# are treated as corruption and auto-dropped in favour of the dominant code.
COMPETING_MIN_PERIODS = 2


def load_observations(input_dir: Path) -> pd.DataFrame:
    """One row per (period, product, item number, customer) from the level_0 CSVs.

    ``period`` is the CSV basename (e.g. ``2026-06`` or ``2024-08_2024-09``).
    Aggregate rows (Customer = SUBTOTAL/TOTAL) and blank product/item cells are
    dropped. Item Number and Product Name are whitespace-stripped; the header
    ``Item Number`` is matched after stripping its own trailing space.
    """
    files = sorted(input_dir.glob("*.csv"))
    if not files:
        raise SystemExit(f"No CSVs found in {input_dir}")

    parts: list[pd.DataFrame] = []
    for f in files:
        df = pd.read_csv(f, dtype=str, keep_default_na=False)
        cols = {c.strip(): c for c in df.columns}
        pn, inum = cols.get("Product Name"), cols.get("Item Number")
        cust = cols.get("Customer")
        if not pn or not inum:
            print(f"  skip {f.name}: missing Product Name / Item Number", file=sys.stderr)
            continue
        sub = pd.DataFrame({
            "period": f.stem,
            "product": df[pn].astype(str).str.strip(),
            "item_number": df[inum].astype(str).str.strip(),
            "customer": (df[cust].astype(str).str.strip() if cust else ""),
        })
        parts.append(sub)

    obs = pd.concat(parts, ignore_index=True)
    obs = obs[~obs["customer"].isin(AGGREGATE_CUSTOMERS)]
    obs = obs[(obs["product"] != "") & (obs["item_number"] != "")]
    obs["slug"] = obs["product"].map(normalize_name)
    obs = obs[obs["slug"] != ""].reset_index(drop=True)
    return obs


def load_known_variants(path: Path) -> dict[str, str]:
    """item_number -> correct_name from the curated known-variants CSV.

    Missing file is fine (returns empty → no merges).
    """
    if not path.exists():
        print(f"  no known-variants file at {path} — merging disabled", file=sys.stderr)
        return {}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {
        r["item_number"].strip(): r["correct_name"].strip()
        for _, r in df.iterrows()
        if r["item_number"].strip() and r["correct_name"].strip()
    }


def build_skus(
    obs: pd.DataFrame, known_variants: dict[str, str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Collapse observations into one canonical row per SKU.

    Two SKUs sharing a canonical Item Number that appears in ``known_variants`` are
    merged into one (the curated ``correct_name``); an unlisted shared code is
    flagged instead. Returns (skus, review). ``period`` stems are lexically
    sortable ``YYYY-MM``, so ``max`` is the most recent.
    """
    review_rows: list[dict] = []

    # Pass 1: canonical Item Number per slug, by distinct-period coverage.
    canonical: dict[str, str] = {}
    coverage_by_slug: dict[str, pd.Series] = {}
    for slug, grp in obs.groupby("slug"):
        valid = grp[grp["item_number"].str.fullmatch(VALID_ITEM_NUMBER)]
        malformed = sorted(set(grp["item_number"]) - set(valid["item_number"]))
        cov = valid.groupby("item_number")["period"].nunique().sort_values(ascending=False)
        coverage_by_slug[slug] = cov
        pname = grp.loc[grp["period"] == grp["period"].max(), "product"].iloc[0]

        if malformed:
            review_rows.append({"Normalized Name": slug, "Product Name": pname,
                                "issue_type": "malformed_dropped", "detail": f"dropped {malformed}"})
        canonical[slug] = "" if cov.empty else cov.index[0]
        if cov.empty:
            review_rows.append({"Normalized Name": slug, "Product Name": pname,
                                "issue_type": "no_valid_item_number", "detail": f"all malformed: {malformed}"})
        else:
            runner_up = cov.iloc[1:][cov.iloc[1:] >= COMPETING_MIN_PERIODS]
            if len(runner_up):
                review_rows.append({"Normalized Name": slug, "Product Name": pname,
                                    "issue_type": "competing_item_numbers",
                                    "detail": "; ".join(f"{i}={n}p" for i, n in cov.items())})

    # Pass 2: group slugs sharing a canonical code; merge those the list approves.
    slugs_by_code: dict[str, list[str]] = defaultdict(list)
    for slug, code in canonical.items():
        if code:
            slugs_by_code[code].append(slug)

    # slug -> merge-group key (the canonical code when merged, else the slug itself)
    group_key: dict[str, str] = {slug: slug for slug in canonical}
    merged_reason: dict[str, str] = {}
    for code, slugs in slugs_by_code.items():
        if len(slugs) <= 1:
            continue
        if code in known_variants:
            for slug in slugs:
                group_key[slug] = f"__code__{code}"
            correct = known_variants[code]
            review_rows.append({"Normalized Name": normalize_name(correct), "Product Name": correct,
                                "issue_type": "merged_variant",
                                "detail": f"{code} merged {sorted(slugs)} -> {correct!r}"})
        else:
            for slug in slugs:
                merged_reason[slug] = code  # unlisted shared code -> flag
                pname = obs.loc[obs["slug"] == slug, "product"].iloc[-1]
                review_rows.append({"Normalized Name": slug, "Product Name": pname,
                                    "issue_type": "shared_item_number",
                                    "detail": f"{code} also on: {[s for s in slugs if s != slug]}"})

    # Pass 3: emit one row per merge-group, aggregating member observations.
    key_to_slugs: dict[str, list[str]] = defaultdict(list)
    for slug, key in group_key.items():
        key_to_slugs[key].append(slug)

    sku_rows: list[dict] = []
    for key, member_slugs in key_to_slugs.items():
        merged = len(member_slugs) > 1
        grp = obs[obs["slug"].isin(member_slugs)]
        latest_period = grp["period"].max()
        if merged:
            code = key.removeprefix("__code__")
            name = known_variants[code]
            ref = code
        else:
            slug = member_slugs[0]
            name = grp.loc[grp["period"] == latest_period, "product"].iloc[0]
            ref = canonical[slug]

        reasons: list[str] = []
        if not ref:
            reasons.append("no_valid_item_number")
        if any(s in merged_reason for s in member_slugs):
            reasons.append("shared_item_number")
        if any(
            len(coverage_by_slug[s].iloc[1:][coverage_by_slug[s].iloc[1:] >= COMPETING_MIN_PERIODS])
            for s in member_slugs
        ):
            reasons.append("competing_item_numbers")
        # union of candidate codes across members, by coverage
        cand = pd.concat([coverage_by_slug[s] for s in member_slugs]).groupby(level=0).sum().sort_values(ascending=False)
        resellers = sorted(c for c in grp["customer"].unique() if c and c != "(none)")

        sku_rows.append({
            "Product Name": name,
            "Normalized Name": normalize_name(name),
            "Customer Reference": ref,
            "months_seen": grp["period"].nunique(),
            "first_period": grp["period"].min(),
            "last_period": latest_period,
            "resellers": ", ".join(resellers),
            "item_number_candidates": "; ".join(f"{i}={n}p" for i, n in cand.items()),
            "merged_from": ", ".join(sorted(member_slugs)) if merged else "",
            "needs_review": bool(reasons),
            "review_reasons": ", ".join(dict.fromkeys(reasons)),
        })

    skus = pd.DataFrame(sku_rows).sort_values("Normalized Name").reset_index(drop=True)
    review = pd.DataFrame(
        review_rows, columns=["Normalized Name", "Product Name", "issue_type", "detail"]
    ).sort_values(["issue_type", "Normalized Name"]).reset_index(drop=True)
    return skus, review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
    parser.add_argument("--known-variants", default=DEFAULT_KNOWN_VARIANTS, type=Path)
    args = parser.parse_args()

    obs = load_observations(args.input_dir)
    print(f"{len(obs)} product observations across {obs['period'].nunique()} periods", file=sys.stderr)

    known = load_known_variants(args.known_variants)
    print(f"{len(known)} known variant code(s) loaded", file=sys.stderr)

    skus, review = build_skus(obs, known)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    skus_path = args.output_dir / "skus.csv"
    review_path = args.output_dir / "review.csv"
    skus.to_csv(skus_path, index=False)
    review.to_csv(review_path, index=False)

    flagged = int(skus["needs_review"].sum())
    no_ref = int((skus["Customer Reference"] == "").sum())
    print(f"\nWrote {skus_path} ({len(skus)} unique SKUs)", file=sys.stderr)
    print(f"  needs review: {flagged}  |  no valid Customer Reference: {no_ref}", file=sys.stderr)
    print(f"Wrote {review_path} ({len(review)} issue rows)", file=sys.stderr)
    for issue, n in review["issue_type"].value_counts().items():
        print(f"    {issue}: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
