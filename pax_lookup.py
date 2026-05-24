"""Lookup helper for PAX codes and Customer References by Normalized Name.

Reads the level_0_match_pax_codes output and exposes a small dict-based
API for joining product names to their assigned PAX code / customer
reference. Use ``normalize.normalize_name`` to derive the lookup key
from a workbook's raw Product Name.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DEFAULT_PAX_CSV = Path("data/level_0_match_pax_codes/royalty_pax_match.csv")


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
