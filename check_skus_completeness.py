"""Cross-check that level_1_extract_skus/skus.csv covers every origin SKU.

Guards against silent loss between the source CSVs
(data/level_0_export_royalty_csvs/*.csv) and the unique-SKU list
(data/level_1_extract_skus/skus.csv). Reuses ``load_observations`` from
level_1_extract_skus so parsing can't drift out of sync with the extractor.

Two checks:

  1. Product completeness (PASS/FAIL) — every distinct normalized product name in
     the origin CSVs must appear in skus.csv, either as its own ``Normalized Name``
     or inside a merged row's ``merged_from``. A missing one is a real gap → exit 1.

  2. Item-Number reconciliation (informational) — every valid Item Number
     (``^[EL]\\d{5}$``) seen in the origin is classified as either used as a
     ``Customer Reference`` in skus.csv, or intentionally dropped (a dominance
     loser / merged-away code), with the code that won shown for context. This is
     an audit of the corruption handling, not a pass/fail.

Exit code is non-zero only when a product is missing (check 1) or a data
inconsistency is found (a Customer Reference not present in the origin).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from level_1_extract_skus import DEFAULT_INPUT_DIR, load_observations

DEFAULT_SKUS = Path("data/level_1_extract_skus/skus.csv")
VALID_ITEM_NUMBER = re.compile(r"^[EL]\d{5}$")


def represented_slugs(skus: pd.DataFrame) -> dict[str, str]:
    """Map every origin slug represented in skus.csv -> the row's Product Name.

    Includes each row's own ``Normalized Name`` plus any slugs listed in
    ``merged_from`` (the source spellings a merged SKU absorbed).
    """
    out: dict[str, str] = {}
    has_merged = "merged_from" in skus.columns
    for _, r in skus.iterrows():
        name = r["Product Name"]
        out[r["Normalized Name"]] = name
        if has_merged and isinstance(r["merged_from"], str) and r["merged_from"].strip():
            for slug in (s.strip() for s in r["merged_from"].split(",")):
                if slug:
                    out[slug] = name
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path)
    parser.add_argument("--skus", default=DEFAULT_SKUS, type=Path)
    args = parser.parse_args()

    obs = load_observations(args.input_dir)
    skus = pd.read_csv(args.skus, dtype=str, keep_default_na=False)

    # --- Check 1: product completeness -------------------------------------
    origin_slugs = {s: obs.loc[obs["slug"] == s, "product"].iloc[-1] for s in obs["slug"].unique()}
    covered = represented_slugs(skus)
    missing = sorted(s for s in origin_slugs if s not in covered)

    print(f"Origin distinct products : {len(origin_slugs)}", file=sys.stderr)
    print(f"skus.csv rows            : {len(skus)}", file=sys.stderr)
    print(f"Origin products covered  : {len(origin_slugs) - len(missing)}/{len(origin_slugs)}", file=sys.stderr)
    if missing:
        print(f"\nFAIL — {len(missing)} origin product(s) MISSING from skus.csv:", file=sys.stderr)
        for s in missing:
            print(f"  {s!r}  (e.g. {origin_slugs[s]!r})", file=sys.stderr)
    else:
        print("PASS — every origin product is represented.", file=sys.stderr)

    # --- Check 2: Item-Number reconciliation -------------------------------
    valid = obs[obs["item_number"].str.fullmatch(VALID_ITEM_NUMBER)]
    code_users: dict[str, set[str]] = defaultdict(set)
    for _, r in valid.iterrows():
        code_users[r["item_number"]].add(r["slug"])
    ref_codes = {r["Customer Reference"] for _, r in skus.iterrows() if r["Customer Reference"]}

    # what won for each slug, so a dropped code shows the code that replaced it
    won_by_slug = {slug: r["Customer Reference"] for _, r in skus.iterrows()
                   for slug in [r["Normalized Name"], *(
                       [x.strip() for x in r["merged_from"].split(",")]
                       if "merged_from" in skus.columns and isinstance(r["merged_from"], str) else [])]
                   if slug}

    dropped = sorted(c for c in code_users if c not in ref_codes)
    print(f"\nItem-Number reconciliation: {len(code_users)} valid codes, "
          f"{len(ref_codes)} used as Customer Reference, {len(dropped)} dropped.", file=sys.stderr)
    for code in dropped:
        users = sorted(code_users[code])
        winners = sorted({won_by_slug.get(u, "?") for u in users})
        print(f"  {code}: dropped — used by {users}; those SKUs now reference {winners}", file=sys.stderr)

    # A Customer Reference that never appears in the origin would signal a bug.
    orphan_refs = sorted(ref_codes - set(code_users))
    if orphan_refs:
        print(f"\nWARNING — {len(orphan_refs)} Customer Reference(s) not found in origin: {orphan_refs}", file=sys.stderr)

    ok = not missing and not orphan_refs
    print(f"\n{'OK' if ok else 'PROBLEMS FOUND'}.", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
