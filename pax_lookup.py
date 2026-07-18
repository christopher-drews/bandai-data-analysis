"""Lookup helper for PAX codes and Customer References by Normalized Name.

Reads the level_2_enrich_pax_codes output (skus_enriched.csv) and exposes a
small dict-based API for joining product names to their assigned PAX code /
customer reference. Use ``normalize.normalize_name`` to derive the lookup key
from a workbook's raw Product Name, then ``build_slug_alias_map`` to fold any
merged spelling variants onto their canonical SKU slug first.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DEFAULT_PAX_CSV = Path("data/level_2_enrich_pax_codes/skus_enriched.csv")


def build_pax_lookup(path: Path = DEFAULT_PAX_CSV) -> dict[str, tuple[str, str]]:
    """Map Normalized Name -> (paxCode, Customer Reference).

    Rows where paxCode is blank are still included (with paxCode = "")
    so callers can distinguish "known product, no PAX yet" from
    "unknown product entirely". Empty cells are normalized to "".
    """
    df = pd.read_csv(path, dtype=str).fillna("")
    if "Customer Reference" not in df.columns:
        df["Customer Reference"] = ""

    lookup: dict[str, tuple[str, str]] = {}
    for _, row in df.iterrows():
        slug = row["Normalized Name"]
        if not slug:
            continue
        lookup[slug] = (row["paxCode"], row["Customer Reference"])
    return lookup


def build_slug_alias_map(path: Path = DEFAULT_PAX_CSV) -> dict[str, str]:
    """Map each merged-away variant slug -> its canonical SKU slug.

    Part 1 (level_1_extract_skus) collapses spelling variants that share an Item
    Number into one SKU, recording the source slugs in ``merged_from``. The raw
    level_0 CSVs still carry both spellings, so callers must fold the variant slug
    onto the canonical ``Normalized Name`` before grouping — otherwise a SKU's SRP
    or promo history splits across the rename. Identity entries are omitted; use
    ``alias_map.get(slug, slug)``.
    """
    df = pd.read_csv(path, dtype=str).fillna("")
    amap: dict[str, str] = {}
    if "merged_from" not in df.columns:
        return amap
    for _, row in df.iterrows():
        canon = row["Normalized Name"]
        merged = row["merged_from"].strip()
        if not merged:
            continue
        for src in (s.strip() for s in merged.split(",")):
            if src and src != canon:
                amap[src] = canon
    return amap
