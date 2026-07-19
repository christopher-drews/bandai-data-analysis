"""Upload bucket-model promotion campaigns into LootVault (direct API).

Reads the grouped campaigns from
``data/level_2_build_promotion_campaigns/promotion_campaigns.json`` (Phase 3.5)
and POSTs **one campaign per group** to
    /api/v1/orgs/{org_id}/promotions

LootVault's promotion model is the **bucket model** (per-SKU discounts within a
campaign, playasia/lootvault#1541): a campaign is a header (name, dates, reseller
scope) over one or more **scope entries**, each carrying its own discount:

    {
      "name": ..., "startDate": ..., "endDate": ...,
      "resellers": [<reseller org ids>],        # omitted => all resellers
      "skus": [ { "skuIds": [...], "discountPercentage": <pct> }, ... ]
    }

So SKUs sharing one discount are grouped into a single scope entry, and a
campaign that mixes discounts emits one scope entry per distinct discount. The
header ``discountPercentage`` is optional UI metadata (never read for pricing)
and is omitted here — the per-SKU scope discount is authoritative.

This replaces the old per-row uploader, which POSTed one single-line promotion
per SKU to ``/api/v1/supplier/{org_id}/promotions`` — maximal fragmentation and
the pre-bucket ``lines[]`` body shape.

Joins + scope resolution
------------------------
Each campaign SKU is a ``Normalized Name`` slug. Catalog items (fetched from
``/api/v1/lv-team/catalog``, filtered to the supplier) are normalized with
``normalize.normalize_name`` and indexed by slug → LootVault SKU id. A campaign
SKU with no catalog match is dropped (with a warning); a campaign whose SKUs all
drop is skipped.

``resellers`` in the JSON is a list of scenario reseller **aliases**
(``["heybox"]`` / ``["sonkwo"]``) or ``null`` (All). Aliases are matched
case-insensitively against the reseller **display names** attached to the
supplier's catalog SKUs to recover reseller org ids; ``null`` => the header
``resellers`` field is omitted (all resellers). ``discount_percentage`` in the
JSON is already a percentage (e.g. ``"20"`` = 20% off), used verbatim.

Note: the create endpoint normally requires startDate >= today and admin
privileges. Historical runs will fail unless the caller is admin and the server
permits backdating.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

from normalize import normalize_name

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
DEFAULT_CAMPAIGNS = Path("data/level_2_build_promotion_campaigns/promotion_campaigns.json")
CATALOG_PAGE_SIZE = 1000
REQUEST_DELAY_S = 0.1
TIMEOUT_S = 30


def fetch_catalog(
    session: requests.Session, host: str, org_id: str, headers: dict
) -> dict[str, tuple[str, dict[str, str]]]:
    """Return {normalized_name: (skuId, {reseller_name_lower: reseller_id})}.

    Pages through /api/v1/lv-team/catalog filtered to this supplier so each
    SKU is returned alongside its attached resellers (id + display name).
    Catalog item names are normalized with ``normalize.normalize_name`` so
    they match the ``Normalized Name`` slugs the level_1/level_2 extracts emit.
    """
    base = f"https://{host}/api/v1/lv-team/catalog"
    by_name: dict[str, tuple[str, dict[str, str]]] = {}
    duplicates: dict[str, list[str]] = defaultdict(list)
    offset = 0
    while True:
        resp = session.get(
            base,
            params={
                "supplier": org_id,
                "offset": offset,
                "limit": CATALOG_PAGE_SIZE,
            },
            headers=headers,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        for item in items:
            slug = normalize_name(item.get("name"))
            sku = item.get("id")
            if not slug or not sku:
                continue
            resellers = {
                (r.get("name") or "").strip().lower(): r["id"]
                for r in item.get("resellers", [])
                if r.get("id")
            }
            if slug in by_name and by_name[slug][0] != sku:
                duplicates[slug].append(sku)
            by_name[slug] = (sku, resellers)
        offset += len(items)
        if len(items) < CATALOG_PAGE_SIZE or offset >= payload.get("total", offset):
            break

    for slug, skus in duplicates.items():
        print(f"  warn: normalized name {slug!r} maps to multiple SKUs: {[by_name[slug][0], *skus]}", file=sys.stderr)
    return by_name


def global_reseller_map(catalog: dict[str, tuple[str, dict[str, str]]]) -> dict[str, str]:
    """Union every catalog SKU's resellers into one {reseller_name_lower: id} map.

    A reseller org has one id across the catalog, so header-level reseller scope
    (per campaign) can be resolved without a per-SKU lookup.
    """
    out: dict[str, str] = {}
    for _sku_id, resellers in catalog.values():
        out.update(resellers)
    return out


def resolve_resellers(
    aliases: list[str] | None, reseller_ids: dict[str, str]
) -> tuple[list[str] | None, str | None]:
    """Map campaign reseller aliases -> org ids. Returns (ids_or_None, error).

    ``None``/empty aliases => all resellers (``(None, None)``, header omitted).
    An unknown alias yields ``(None, "<message>")`` so the caller can skip.
    """
    if not aliases:
        return None, None
    ids: list[str] = []
    for alias in aliases:
        rid = reseller_ids.get(alias.strip().lower())
        if not rid:
            return None, f"reseller alias {alias!r} not among catalog resellers {sorted(reseller_ids)}"
        ids.append(rid)
    return ids, None


def build_scopes(
    skus: list[dict], catalog: dict[str, tuple[str, dict[str, str]]]
) -> tuple[list[dict], list[str]]:
    """Group a campaign's SKUs by discount into scope entries.

    Returns (scopes, missing_slugs). Each scope is
    ``{"skuIds": [...], "discountPercentage": <float>}``; scopes are ordered by
    discount. ``missing_slugs`` are SKUs with no catalog match (dropped).
    """
    by_discount: dict[str, list[str]] = defaultdict(list)
    missing: list[str] = []
    for entry in skus:
        slug = entry["sku"]
        hit = catalog.get(slug)
        if not hit:
            missing.append(slug)
            continue
        by_discount[str(entry["discount_percentage"])].append(hit[0])

    scopes = [
        {"skuIds": sorted(sku_ids), "discountPercentage": float(pct)}
        for pct, sku_ids in sorted(by_discount.items(), key=lambda kv: float(kv[0]))
    ]
    return scopes, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaigns", default=DEFAULT_CAMPAIGNS, type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID)
    parser.add_argument("--token", required=True, help="Bearer JWT")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    campaigns = json.loads(args.campaigns.read_text(encoding="utf-8"))

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    session = requests.Session()

    print("Fetching supplier catalog...", file=sys.stderr)
    catalog = fetch_catalog(session, args.host, args.org_id, headers)
    reseller_ids = global_reseller_map(catalog)
    print(f"  {len(catalog)} SKUs indexed by normalized name; "
          f"resellers: {sorted(reseller_ids)}", file=sys.stderr)

    url = f"https://{args.host}/api/v1/orgs/{args.org_id}/promotions"
    ok = fail = skipped = dropped_skus = 0
    for camp in campaigns:
        name = camp["name"]
        start_date = camp["start_date"]
        end_date = camp["end_date"]

        resellers, err = resolve_resellers(camp.get("resellers"), reseller_ids)
        if err:
            print(f"  skip {name!r}: {err}", file=sys.stderr)
            skipped += 1
            continue

        scopes, missing = build_scopes(camp["skus"], catalog)
        if missing:
            dropped_skus += len(missing)
            print(f"  warn {name!r}: {len(missing)} SKU(s) not in catalog, dropped: "
                  f"{missing}", file=sys.stderr)
        if not scopes:
            print(f"  skip {name!r}: no SKUs resolved to the catalog", file=sys.stderr)
            skipped += 1
            continue

        body: dict = {
            "name": name,
            "startDate": start_date,
            "endDate": end_date,
            "skus": scopes,
        }
        if resellers:
            body["resellers"] = resellers

        n_skus = sum(len(s["skuIds"]) for s in scopes)
        scope_label = "All" if not resellers else "+".join(resellers)
        prefix = (
            f"{name!r} {start_date}->{end_date} [{scope_label}] "
            f"{n_skus} SKU(s) in {len(scopes)} discount bucket(s)"
        )
        if args.dry_run:
            print(f"  [dry-run] POST {prefix}", file=sys.stderr)
            continue

        resp = session.post(url, json=body, headers=headers, timeout=TIMEOUT_S)
        if resp.ok:
            print(f"  ok   {prefix}", file=sys.stderr)
            ok += 1
        else:
            print(f"  FAIL {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(f"\nDone. ok={ok} fail={fail} skipped={skipped} "
          f"dropped_skus={dropped_skus} total_campaigns={len(campaigns)}", file=sys.stderr)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
