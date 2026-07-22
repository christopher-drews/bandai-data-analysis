"""Assign a paxCode + steamId to every unique SKU (offline association).

Part 2 of the SKU pipeline. Reads the clean SKU list from
data/level_1_extract_skus/skus.csv and the Playasia catalog dumped by
extract_bandai_products.py (data/bandai_catalog.csv: paxCode, label, steamId),
and matches each SKU to a catalog product **by name** — the royalty report
carries no paxCode/steamId, so the label is the only bridge.

Matching passes (first hit wins), reusing the qualifier/base-game logic from
level_0_match_pax_codes:
  1. exact      — normalized Product Name == normalized catalog label
  2. fuzzy      — best SequenceMatcher ratio >= SIMILARITY_THRESHOLD (0.95)
  3. base_game  — strip edition qualifiers ("Deluxe", "Season 2", ...) until the
                  slug matches a catalog label

A catalog label is **not unique** (regional/edition variants share a label, and
some share a steamId), so a matched label can carry several paxCodes. The primary
is the entry with a steamId when available, else the lexicographically smallest
paxCode; the rest are recorded in ``related_pax_codes``.

Manual overrides
----------------
A manual match always wins. Rows in data/sku_paxcode_overrides.csv
(``Normalized Name -> paxCode`` [+ optional ``steamId``]) override the auto-match
with ``pax_match_status = manual``; steamId/label are filled from the catalog by
that paxCode when not given. On first run (file absent) a template of the
still-blank SKUs is seeded there for you to fill; it is never overwritten once it
exists, so re-running applies whatever you've entered.

paxCode uniqueness across SKUs is then enforced: if two SKUs resolve to the same
paxCode, the higher-confidence match (manual > exact > fuzzy > base_game) keeps it;
an auto-match loser's paxCode is blanked and flagged. A manual duplicate is flagged
but never blanked — it is treated as deliberate.

**Every SKU is always emitted**, with or without a paxCode.

Outputs (data/level_2_enrich_pax_codes/):
  - skus_enriched.csv  ALL SKUs + paxCode, steamId, matched_label,
                       pax_match_status, pax_match_score, related_pax_codes,
                       pax_needs_review, pax_review_reason
  - unmatched.csv      SKUs with no paxCode or a flagged conflict, for review
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

from level_0_match_pax_codes import SIMILARITY_THRESHOLD, find_base_game_match
from normalize import normalize_name

DEFAULT_SKUS = Path("data/level_1_extract_skus/skus.csv")
DEFAULT_CATALOG = Path("data/bandai_catalog.csv")
DEFAULT_OVERRIDES = Path("data/sku_paxcode_overrides.csv")
DEFAULT_OUTPUT_DIR = Path("data/level_2_enrich_pax_codes")

# "manual" wins any auto-match and is never blanked by uniqueness enforcement.
MATCH_PRIORITY = {"manual": -1, "exact": 0, "fuzzy": 1, "base_game": 2, "unmatched": 9}

OVERRIDE_COLUMNS = ["Normalized Name", "Product Name", "Customer Reference",
                    "reason", "paxCode", "steamId", "note"]


def load_catalog(path: Path) -> dict[str, list[dict]]:
    """normalized label -> [{paxCode, steamId, label}, ...] (label is not unique)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    by_slug: dict[str, list[dict]] = defaultdict(list)
    for _, r in df.iterrows():
        slug = normalize_name(r["label"])
        if not slug or not r["paxCode"]:
            continue
        by_slug[slug].append({
            "paxCode": r["paxCode"].strip(),
            "steamId": r.get("steamId", "").strip(),
            "label": r["label"].strip(),
        })
    return by_slug


def load_catalog_by_pax(path: Path) -> dict[str, dict]:
    """paxCode -> {steamId, label} for filling manual overrides from the catalog."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {
        r["paxCode"].strip(): {"steamId": r.get("steamId", "").strip(), "label": r["label"].strip()}
        for _, r in df.iterrows() if r["paxCode"].strip()
    }


def load_overrides(path: Path) -> dict[str, dict]:
    """Normalized Name -> {paxCode, steamId, customer_reference} from the override file.

    A row is applied when it carries a paxCode OR a Customer Reference:
      - ``paxCode``           -> a manual paxCode match (wins over auto-matching).
      - ``Customer Reference`` -> the SKU's customer_reference, used when the royalty
        report has no Item Number of its own (new/upcoming titles). Overrides the
        blank value carried from skus.csv so the SKU can flow into the scenario's
        ``customer_reference`` and the add-sales CSVs.
    A row may set either or both; rows with neither are template placeholders.
    Missing file is fine (returns empty).
    """
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        slug = r.get("Normalized Name", "").strip()
        pax = r.get("paxCode", "").strip()
        ref = r.get("Customer Reference", "").strip()
        if slug and (pax or ref):
            out[slug] = {"paxCode": pax, "steamId": r.get("steamId", "").strip(),
                         "customer_reference": ref}
    return out


def pick_primary(entries: list[dict]) -> tuple[dict, list[str]]:
    """Primary entry (prefers one with a steamId, then smallest paxCode) + the rest."""
    ordered = sorted(entries, key=lambda e: (e["steamId"] == "", e["paxCode"]))
    primary = ordered[0]
    related = [e["paxCode"] for e in ordered[1:]]
    return primary, related


def match_sku(slug: str, by_slug: dict[str, list[dict]], catalog_slugs: list[str],
              catalog_slug_set: set[str]) -> tuple[str, float | None, str | None]:
    """Return (match_status, score, matched_slug) for one SKU slug."""
    if slug in catalog_slug_set:
        return "exact", 1.0, slug
    best, best_score = None, 0.0
    for cand in catalog_slugs:
        score = SequenceMatcher(None, slug, cand).ratio()
        if score > best_score:
            best, best_score = cand, score
    if best is not None and best_score >= SIMILARITY_THRESHOLD:
        return "fuzzy", round(best_score, 3), best
    result = find_base_game_match(slug, catalog_slug_set)
    if result is not None:
        return "base_game", None, result[0]
    return "unmatched", None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skus", default=DEFAULT_SKUS, type=Path)
    parser.add_argument("--catalog", default=DEFAULT_CATALOG, type=Path)
    parser.add_argument("--overrides", default=DEFAULT_OVERRIDES, type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
    args = parser.parse_args()

    skus = pd.read_csv(args.skus, dtype=str, keep_default_na=False)
    by_slug = load_catalog(args.catalog)
    by_pax = load_catalog_by_pax(args.catalog)
    catalog_slugs = list(by_slug.keys())
    catalog_slug_set = set(catalog_slugs)
    overrides = load_overrides(args.overrides)
    print(f"{len(skus)} SKUs, {len(catalog_slugs)} distinct catalog labels, "
          f"{len(overrides)} manual override(s)", file=sys.stderr)

    rows: list[dict] = []
    for _, sku in skus.iterrows():
        slug = sku["Normalized Name"]
        pax = steam = matched_label = ""
        related: list[str] = []
        reasons: list[str] = []

        override = overrides.get(slug)
        if override is not None and override["paxCode"]:
            # Manual paxCode match wins outright.
            status, score = "manual", None
            pax = override["paxCode"]
            cat = by_pax.get(pax, {})
            steam = override["steamId"] or cat.get("steamId", "")
            matched_label = cat.get("label", "")
        else:
            status, score, matched_slug = match_sku(slug, by_slug, catalog_slugs, catalog_slug_set)
            if matched_slug is not None:
                primary, related = pick_primary(by_slug[matched_slug])
                pax, steam, matched_label = primary["paxCode"], primary["steamId"], primary["label"]
            else:
                reasons.append("unmatched")

        row = {
            **sku.to_dict(),
            "paxCode": pax,
            "steamId": steam,
            "matched_label": matched_label,
            "pax_match_status": status,
            "pax_match_score": score if score is not None else "",
            "related_pax_codes": ", ".join(related),
            "_priority": MATCH_PRIORITY[status],
            "pax_needs_review": bool(reasons),
            "pax_review_reason": ", ".join(reasons),
        }
        # A Customer Reference override supplies a customer_reference the royalty
        # report lacked (new/upcoming titles with no Item Number) — it wins over
        # the blank value carried from skus.csv.
        if override is not None and override["customer_reference"]:
            row["Customer Reference"] = override["customer_reference"]
        rows.append(row)

    enriched = pd.DataFrame(rows)

    # Enforce paxCode uniqueness across SKUs: best-priority match keeps the code.
    # A manual override is authoritative — it is flagged if duplicated but never blanked.
    for pax, grp in enriched[enriched["paxCode"] != ""].groupby("paxCode"):
        if len(grp) == 1:
            continue
        keep_idx = grp.sort_values("_priority").index[0]
        for idx in grp.index:
            if idx == keep_idx:
                continue
            if enriched.at[idx, "pax_match_status"] == "manual":
                enriched.at[idx, "pax_needs_review"] = True
                enriched.at[idx, "pax_review_reason"] = ", ".join(
                    filter(None, [enriched.at[idx, "pax_review_reason"], f"duplicate_paxCode:{pax}"])
                )
                continue
            losers = enriched.at[idx, "related_pax_codes"]
            enriched.at[idx, "related_pax_codes"] = ", ".join(filter(None, [enriched.at[idx, "paxCode"], losers]))
            enriched.at[idx, "paxCode"] = ""
            enriched.at[idx, "pax_needs_review"] = True
            enriched.at[idx, "pax_review_reason"] = ", ".join(
                filter(None, [enriched.at[idx, "pax_review_reason"], f"duplicate_paxCode:{pax}"])
            )

    enriched = enriched.drop(columns="_priority")

    # Seed the manual-override template on first run (never overwrite existing entries).
    if not args.overrides.exists():
        blank = enriched[enriched["paxCode"] == ""]
        template = blank[["Normalized Name", "Product Name", "Customer Reference"]].copy()
        template["reason"] = blank["pax_review_reason"]
        template["paxCode"] = ""
        template["steamId"] = ""
        template["note"] = ""
        args.overrides.parent.mkdir(parents=True, exist_ok=True)
        template[OVERRIDE_COLUMNS].to_csv(args.overrides, index=False)
        print(f"Seeded override template {args.overrides} ({len(template)} rows to fill)", file=sys.stderr)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    enriched_path = args.output_dir / "skus_enriched.csv"
    unmatched_path = args.output_dir / "unmatched.csv"
    enriched.to_csv(enriched_path, index=False)
    review = enriched[enriched["pax_needs_review"]]
    review[["Product Name", "Normalized Name", "Customer Reference", "paxCode",
            "related_pax_codes", "pax_match_status", "pax_review_reason"]].to_csv(unmatched_path, index=False)

    with_pax = int((enriched["paxCode"] != "").sum())
    with_steam = int((enriched["steamId"] != "").sum())
    print(f"\nWrote {enriched_path} ({len(enriched)} SKUs)", file=sys.stderr)
    print(f"  with paxCode : {with_pax}/{len(enriched)}", file=sys.stderr)
    print(f"  with steamId : {with_steam}/{len(enriched)}", file=sys.stderr)
    for status, n in enriched["pax_match_status"].value_counts().items():
        print(f"    {status}: {n}", file=sys.stderr)
    print(f"Wrote {unmatched_path} ({len(review)} needing review)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
