"""Extract which resellers carry each SKU, from the level_0 CSVs.

Walks every CSV in data/level_0_export_royalty_csvs/ that has a ``Customer``
column (2025-07 onward — earlier sheets have no reseller breakdown), and records
every (SKU, reseller) pair that appears. Product names are normalized and folded
onto their canonical SKU slug (merged spelling variants). Only Heybox and Sonkwo
are kept; Alibaba is out of scope.

This drives per-reseller ``authorize_skus`` in the scenario relationships — a SKU
is authorized to a reseller only if that reseller actually carried it, so the
Heybox/Sonkwo catalogs match the report (not a blanket "all").

Output: data/level_1_extract_reseller_skus/reseller_skus.csv with columns
    Normalized Name, Product Name, reseller
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from normalize import normalize_name
from pax_lookup import build_slug_alias_map

DEFAULT_INPUT_DIR = Path("data/level_0_export_royalty_csvs")
DEFAULT_OUTPUT = Path("data/level_1_extract_reseller_skus/reseller_skus.csv")
RESELLERS = {"Heybox", "Sonkwo"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    alias_map = build_slug_alias_map()

    seen: dict[tuple[str, str], str] = {}  # (slug, reseller) -> latest product name
    files_with_customer = 0
    for f in sorted(args.input_dir.glob("*.csv")):
        df = pd.read_csv(f, dtype=str, keep_default_na=False)
        cols = {c.strip(): c for c in df.columns}
        if "Customer" not in cols or "Product Name" not in cols:
            continue
        files_with_customer += 1
        sub = pd.DataFrame({
            "product": df[cols["Product Name"]].str.strip(),
            "cust": df[cols["Customer"]].str.strip(),
        })
        sub = sub[sub["cust"].isin(RESELLERS) & (sub["product"] != "")]
        for _, r in sub.iterrows():
            slug = alias_map.get(normalize_name(r["product"]), normalize_name(r["product"]))
            if slug:
                seen[(slug, r["cust"])] = r["product"]  # last spelling wins

    rows = [{"Normalized Name": slug, "Product Name": name, "reseller": reseller}
            for (slug, reseller), name in seen.items()]
    out = pd.DataFrame(rows, columns=["Normalized Name", "Product Name", "reseller"])
    out = out.sort_values(["reseller", "Normalized Name"]).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output, index=False)

    print(f"Wrote {args.output} ({len(out)} (SKU, reseller) pairs "
          f"from {files_with_customer} reseller-attributed months)", file=sys.stderr)
    for reseller in sorted(RESELLERS):
        print(f"  {reseller}: {(out['reseller'] == reseller).sum()} SKUs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
