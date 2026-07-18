"""Upload promotion runs from data/level_1_extract_promo_history/product_promo_history.csv into LootVault.

For each row, POSTs a single-line promotion campaign to
    /api/v1/supplier/{org_id}/promotions

The campaign carries the shared ``name``/``startDate``/``endDate``; the SKU,
discount, and reseller scope live on its one inline line (``LineSpec``), with
the SKU targeted via ``scope.skuIds``. (The older per-SKU endpoint
``/catalog/{sku_id}/promotions`` is now GET-only and returns 405 on POST.)

Rows are joined to a LootVault SKU by normalizing the catalog item's
``name`` with ``normalize.normalize_name`` and matching it against the
CSV's ``Normalized Name`` column. The ``Customer`` column from
level_1_extract_promo_history is mapped to a reseller scope:

  - "All"       -> applies to every reseller (resellers field omitted)
  - "Heybox"    -> matched case-insensitively against the SKU's resellers
  - "Sonkwo"    -> matched case-insensitively against the SKU's resellers
  - anything else -> skipped with a warning

``Promo Discount`` is a fraction in the CSV (e.g. 0.20 = 20% off) and is
multiplied by 100 for the API, which expects ``discountPercentage`` in
the 0.01–100 range. ``start_date``/``end_date`` are YYYY-MM-DD and used
verbatim. The currency defaults to CNY.

Note: the create endpoint normally requires startDate >= today and admin
privileges. Historical runs will fail unless the caller is admin and the
server permits backdating.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

from normalize import normalize_name

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
DEFAULT_CSV = Path("data/level_1_extract_promo_history/product_promo_history.csv")
DEFAULT_CURRENCY = "CNY"
CATALOG_PAGE_SIZE = 1000
REQUEST_DELAY_S = 0.1
TIMEOUT_S = 30

ALL_CUSTOMERS_TOKEN = "All"


def fetch_catalog(
    session: requests.Session, host: str, org_id: str, headers: dict
) -> dict[str, tuple[str, dict[str, str]]]:
    """Return {normalized_name: (skuId, {reseller_name_lower: reseller_id})}.

    Pages through /api/v1/lv-team/catalog filtered to this supplier so each
    SKU is returned alongside its attached resellers (id + display name).
    Catalog item names are normalized with ``normalize.normalize_name`` so
    they match the ``Normalized Name`` column the level_1 extracts emit.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV, type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID)
    parser.add_argument("--currency", default=DEFAULT_CURRENCY)
    parser.add_argument("--token", required=True, help="Bearer JWT")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with args.csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    session = requests.Session()

    print("Fetching supplier catalog...", file=sys.stderr)
    catalog = fetch_catalog(session, args.host, args.org_id, headers)
    print(f"  {len(catalog)} SKUs indexed by normalized name", file=sys.stderr)

    ok = fail = skipped = 0
    for row in rows:
        slug = (row.get("Normalized Name") or "").strip() or normalize_name(row.get("Product Name"))
        promo_raw = (row.get("Promo Discount") or "").strip()
        customer = (row.get("Customer") or "").strip()
        start_date = (row.get("start_date") or "").strip()
        end_date = (row.get("end_date") or "").strip()
        product = (row.get("Product Name") or "").strip()

        if not slug:
            print(f"  skip {product!r}: no normalized name", file=sys.stderr)
            skipped += 1
            continue
        entry = catalog.get(slug)
        if not entry:
            print(f"  skip {product!r} ({slug}): no matching SKU in catalog", file=sys.stderr)
            skipped += 1
            continue
        sku_id, sku_resellers = entry

        if not (promo_raw and start_date and end_date):
            print(f"  skip {product!r}: missing promo/dates", file=sys.stderr)
            skipped += 1
            continue

        try:
            discount_percentage = float(promo_raw) * 100
        except ValueError:
            print(f"  skip {product!r}: bad Promo Discount value {promo_raw!r}", file=sys.stderr)
            skipped += 1
            continue

        line: dict = {
            "discountPercentage": round(discount_percentage, 4),
            "currencies": [args.currency],
            "scope": {"skuIds": [sku_id]},
        }

        scope = "all-resellers"
        if customer and customer != ALL_CUSTOMERS_TOKEN:
            reseller_id = sku_resellers.get(customer.lower())
            if not reseller_id:
                print(
                    f"  skip {product!r} ({slug}): customer {customer!r} not in "
                    f"SKU resellers {sorted(sku_resellers)}",
                    file=sys.stderr,
                )
                skipped += 1
                continue
            line["resellers"] = [reseller_id]
            scope = f"{customer}={reseller_id}"

        reseller_label = customer if customer and customer != ALL_CUSTOMERS_TOKEN else "All"
        name = f"{product} {start_date} -{round(discount_percentage, 2)}% [{reseller_label}]"
        body: dict = {
            "name": name,
            "startDate": start_date,
            "endDate": end_date,
            "lines": [line],
        }

        prefix = (
            f"{slug} ({sku_id}) {line['discountPercentage']}% "
            f"{start_date}->{end_date} [{scope}]"
        )
        if args.dry_run:
            print(f"  [dry-run] POST {prefix}", file=sys.stderr)
            continue

        url = f"https://{args.host}/api/v1/supplier/{args.org_id}/promotions"
        resp = session.post(url, json=body, headers=headers, timeout=TIMEOUT_S)
        if resp.ok:
            print(f"  ok   {prefix}", file=sys.stderr)
            ok += 1
        else:
            print(f"  FAIL {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(f"\nDone. ok={ok} fail={fail} skipped={skipped} total={len(rows)}", file=sys.stderr)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
