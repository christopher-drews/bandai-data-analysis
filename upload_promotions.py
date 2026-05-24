"""Upload promotion runs from data/level_1_extract_promo_history/product_promo_history.csv into LootVault.

For each row, POSTs to
    /api/v1/supplier/{org_id}/catalog/{sku_id}/promotions

Rows are joined to a LootVault SKU by their ``paxCode`` (resolved against
the supplier catalog's ``paPaxCode`` field). The ``Customer`` column from
level_1_extract_promo_history is mapped to a reseller scope:

  - "All"       -> applies to every reseller (resellers field omitted)
  - "Heybox"    -> matched case-insensitively against the SKU's resellers
  - "Sonkwo"    -> matched case-insensitively against the SKU's resellers
  - anything else -> skipped with a warning

``Promo Discount`` is a fraction in the CSV (e.g. 0.20 = 20% off) and is
multiplied by 100 for the API, which expects ``discountPercentage`` in
the 0.01–100 range. ``start_month``/``end_month`` expand to first-of-month
/ last-of-month dates and the currency defaults to CNY.

Note: the create endpoint normally requires startDate >= today and admin
privileges. Historical runs will fail unless the caller is admin and the
server permits backdating.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

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
    """Return {paPaxCode: (skuId, {reseller_name_lower: reseller_id})}.

    Pages through /api/v1/lv-team/catalog filtered to this supplier so each
    SKU is returned alongside its attached resellers (id + display name).
    """
    base = f"https://{host}/api/v1/lv-team/catalog"
    by_pax: dict[str, tuple[str, dict[str, str]]] = {}
    duplicates: dict[str, list[str]] = defaultdict(list)
    offset = 0
    while True:
        resp = session.get(
            base,
            params={
                "supplier": org_id,
                "has_paxcode": "true",
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
            pax = item.get("paPaxCode")
            sku = item.get("id")
            if not pax or not sku:
                continue
            resellers = {
                (r.get("name") or "").strip().lower(): r["id"]
                for r in item.get("resellers", [])
                if r.get("id")
            }
            if pax in by_pax and by_pax[pax][0] != sku:
                duplicates[pax].append(sku)
            by_pax[pax] = (sku, resellers)
        offset += len(items)
        if len(items) < CATALOG_PAGE_SIZE or offset >= payload.get("total", offset):
            break

    for pax, skus in duplicates.items():
        print(f"  warn: paxCode {pax!r} maps to multiple SKUs: {[by_pax[pax][0], *skus]}", file=sys.stderr)
    return by_pax


def month_bounds(start_month: str, end_month: str) -> tuple[str, str]:
    """`'2024-08','2024-10'` -> `('2024-08-01','2024-10-31')`."""
    sy, sm = (int(x) for x in start_month.split("-"))
    ey, em = (int(x) for x in end_month.split("-"))
    last_day = calendar.monthrange(ey, em)[1]
    return f"{sy:04d}-{sm:02d}-01", f"{ey:04d}-{em:02d}-{last_day:02d}"


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
    print(f"  {len(catalog)} SKUs with paPaxCode", file=sys.stderr)

    ok = fail = skipped = 0
    for row in rows:
        pax = (row.get("paxCode") or "").strip()
        promo_raw = (row.get("Promo Discount") or "").strip()
        customer = (row.get("Customer") or "").strip()
        start_month = (row.get("start_month") or "").strip()
        end_month = (row.get("end_month") or "").strip()
        product = (row.get("Product Name") or "").strip()

        if not pax:
            print(f"  skip {product!r}: no paxCode", file=sys.stderr)
            skipped += 1
            continue
        entry = catalog.get(pax)
        if not entry:
            print(f"  skip {product!r} ({pax}): no matching SKU in catalog", file=sys.stderr)
            skipped += 1
            continue
        sku_id, sku_resellers = entry

        if not (promo_raw and start_month and end_month):
            print(f"  skip {product!r}: missing promo/months", file=sys.stderr)
            skipped += 1
            continue

        try:
            discount_percentage = float(promo_raw) * 100
        except ValueError:
            print(f"  skip {product!r}: bad Promo Discount value {promo_raw!r}", file=sys.stderr)
            skipped += 1
            continue

        body: dict = {
            "discountPercentage": round(discount_percentage, 4),
            "startDate": "",
            "endDate": "",
            "currencies": [args.currency],
        }
        body["startDate"], body["endDate"] = month_bounds(start_month, end_month)

        scope = "all-resellers"
        if customer and customer != ALL_CUSTOMERS_TOKEN:
            reseller_id = sku_resellers.get(customer.lower())
            if not reseller_id:
                print(
                    f"  skip {product!r} ({pax}): customer {customer!r} not in "
                    f"SKU resellers {sorted(sku_resellers)}",
                    file=sys.stderr,
                )
                skipped += 1
                continue
            body["resellers"] = [reseller_id]
            scope = f"{customer}={reseller_id}"

        prefix = (
            f"{pax} ({sku_id}) {body['discountPercentage']}% "
            f"{body['startDate']}->{body['endDate']} [{scope}]"
        )
        if args.dry_run:
            print(f"  [dry-run] POST {prefix}", file=sys.stderr)
            continue

        url = f"https://{args.host}/api/v1/supplier/{args.org_id}/catalog/{sku_id}/promotions"
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
