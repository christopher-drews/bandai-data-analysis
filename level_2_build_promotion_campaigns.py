"""Group raw promo-history rows into named campaigns for the scenario builder.

Phase 3.5. Reads the per-row promotion history (one row per SKU x Customer x
window) and the SKU catalog, then collapses rows that share
``(start_date, end_date, discount, reseller-scope)`` into a single campaign
covering many SKUs — the shape the scenario ``promotions[]`` section wants.

This is the campaign *analysis* step: grouping + naming live here, not in the
scenario YAML emitter (level_3_build_scenario.py), so the builder only has to
serialize what this produces.

``Customer`` maps to a reseller scope (All -> every reseller; Heybox/Sonkwo ->
that reseller). Alibaba rows are dropped (out of scope). Rows whose SKU is not
in the catalog are dropped, so single-item detection matches exactly what lands
in the scenario.

Naming
------
Base label: ``"{YYYY-MM} {Customer} {pct}%"`` — e.g. ``"2025-08 Heybox 20%"``.
When a campaign covers exactly ONE SKU, a truncated product suffix is appended
(<=30 chars, trademark + " Edition" stripped):
``"2025-08 Heybox 20% — ELDEN RING NIGHTREIGN"``. This mirrors the live-API
renamer (rename_promotions.py); the two are kept in sync by hand.

Output: ``data/level_2_build_promotion_campaigns/promotion_campaigns.json`` —
a JSON list of campaign objects the builder consumes verbatim:
``{name, start_date, end_date, discount_percentage, resellers, skus}``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

DEFAULT_PROMO = Path("data/level_1_extract_promo_history/product_promo_history.csv")
DEFAULT_SKUS = Path("data/level_1_extract_skus/skus.csv")
DEFAULT_OUTPUT_DIR = Path("data/level_2_build_promotion_campaigns")

# Customer (report reseller) -> scenario reseller aliases. None => all resellers.
# Alibaba is intentionally absent: its rows are dropped.
CUSTOMER_SCOPE: dict[str, list[str] | None] = {
    "All": None,
    "Heybox": ["heybox"],
    "Sonkwo": ["sonkwo"],
}
RESELLER_DISPLAY = {"heybox": "Heybox", "sonkwo": "Sonkwo"}

# Single-item naming. Mirrors rename_promotions.py (kept in sync by hand).
PRODUCT_NAME_MAX = 30
EDITION_SUFFIX_RE = re.compile(
    r"\s+(Digital\s+|Premium\s+|Ultimate\s+|Deluxe\s+|Standard\s+|Collector'?s?\s+)?Edition\s*$",
    re.IGNORECASE,
)
TRADEMARK_RE = re.compile(r"[™®]")


def short_product(name: str) -> str:
    """Strip trademarks and trailing 'Edition' clause, then truncate to PRODUCT_NAME_MAX."""
    cleaned = TRADEMARK_RE.sub("", name or "").strip()
    cleaned = EDITION_SUFFIX_RE.sub("", cleaned).strip()
    if len(cleaned) > PRODUCT_NAME_MAX:
        cleaned = cleaned[: PRODUCT_NAME_MAX - 1].rstrip() + "…"
    return cleaned


def fmt_num(x: float, nd: int = 4) -> str:
    """Trim a number for output: integer when whole, else rounded to nd places."""
    v = round(float(x), nd)
    return str(int(v)) if v == int(v) else str(v)


def build_campaigns(
    promo: pd.DataFrame, alias_set: set[str], product_by_slug: dict[str, str]
) -> tuple[list[dict], dict[str, int]]:
    stats = {"dropped_alibaba": 0, "unknown_customer": 0, "unknown_sku": 0}
    groups: dict[tuple, list[str]] = {}
    for _, r in promo.iterrows():
        customer = r["Customer"]
        if customer == "Alibaba":
            stats["dropped_alibaba"] += 1
            continue
        if customer not in CUSTOMER_SCOPE:
            stats["unknown_customer"] += 1
            continue
        slug = r["Normalized Name"]
        if slug not in alias_set:
            stats["unknown_sku"] += 1
            continue
        resellers = CUSTOMER_SCOPE[customer]
        pct = fmt_num(float(r["Promo Discount"]) * 100)
        key = (r["start_date"], r["end_date"], pct, tuple(resellers) if resellers else None)
        groups.setdefault(key, []).append(slug)

    campaigns: list[dict] = []
    for (sd, ed, pct, res), slugs in groups.items():
        label = "All" if not res else "+".join(RESELLER_DISPLAY.get(x, x) for x in res)
        skus = sorted(set(slugs))
        name = f"{sd[:7]} {label} {pct}%"
        if len(skus) == 1:
            name = f"{name} — {short_product(product_by_slug.get(skus[0], skus[0]))}"
        campaigns.append({
            "name": name,
            "start_date": sd, "end_date": ed, "discount_percentage": pct,
            "resellers": list(res) if res else None,
            "skus": skus,
        })
    campaigns.sort(key=lambda c: (c["start_date"], c["name"], c["skus"][0]))
    return campaigns, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promo", default=DEFAULT_PROMO, type=Path)
    parser.add_argument("--skus", default=DEFAULT_SKUS, type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
    args = parser.parse_args()

    skus = pd.read_csv(args.skus, dtype=str, keep_default_na=False)
    alias_set = set(skus["Normalized Name"])
    product_by_slug = dict(zip(skus["Normalized Name"], skus["Product Name"]))

    promo = pd.read_csv(args.promo, dtype=str, keep_default_na=False)
    campaigns, stats = build_campaigns(promo, alias_set, product_by_slug)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / "promotion_campaigns.json"
    path.write_text(json.dumps(campaigns, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}", file=sys.stderr)

    single = sum(1 for c in campaigns if len(c["skus"]) == 1)
    print(f"  campaigns  -> {len(campaigns)} ({single} single-SKU, named with product)",
          file=sys.stderr)
    print(f"  dropped    -> Alibaba: {stats['dropped_alibaba']}, "
          f"unknown customer: {stats['unknown_customer']}, "
          f"unknown SKU: {stats['unknown_sku']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
