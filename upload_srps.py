"""Upload SRP runs from data/level_1_extract_srp_history/product_srp_history.csv into LootVault.

For each row, POSTs to
    /api/v1/supplier/{org_id}/catalog/{sku_id}/srp

Rows are joined to a LootVault SKU by normalizing the catalog item's
``name`` with ``normalize.normalize_name`` and matching it against the
CSV's ``Normalized Name`` column. ``start_month`` expands to a
first-of-month date; ``end_month`` expands to a last-of-month date or is
omitted entirely if blank (the SRP is still active).

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

from normalize import normalize_name

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
DEFAULT_CSV = Path("data/level_1_extract_srp_history/product_srp_history.csv")
CATALOG_PAGE_SIZE = 1000
REQUEST_DELAY_S = 0.1
TIMEOUT_S = 30


def fetch_name_to_sku(session: requests.Session, host: str, org_id: str, headers: dict) -> dict[str, str]:
    """Page through the lv-team catalog (filtered to this supplier) and return {normalized_name: skuId}.

    Uses /api/v1/lv-team/catalog rather than /supplier/{org_id}/catalog because
    the deployed API currently only allows POST on the supplier-catalog path.
    Catalog item names are normalized with ``normalize.normalize_name`` so they
    match the ``Normalized Name`` column the level_1 extracts emit.
    """
    base = f"https://{host}/api/v1/lv-team/catalog"
    mapping: dict[str, str] = {}
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
            if slug in mapping and mapping[slug] != sku:
                duplicates[slug].append(sku)
            mapping[slug] = sku
        offset += len(items)
        if len(items) < CATALOG_PAGE_SIZE or offset >= payload.get("total", offset):
            break

    for slug, skus in duplicates.items():
        print(f"  warn: normalized name {slug!r} maps to multiple SKUs: {[mapping[slug], *skus]}", file=sys.stderr)
    return mapping


def month_start(month: str) -> str:
    """`'2024-08'` -> `'2024-08-01'`."""
    y, m = (int(x) for x in month.split("-"))
    return f"{y:04d}-{m:02d}-01"


def month_end(month: str) -> str:
    """`'2024-10'` -> `'2024-10-31'`."""
    y, m = (int(x) for x in month.split("-"))
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV, type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID)
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
    name_to_sku = fetch_name_to_sku(session, args.host, args.org_id, headers)
    print(f"  {len(name_to_sku)} SKUs indexed by normalized name", file=sys.stderr)

    ok = fail = skipped = 0
    for row in rows:
        slug = (row.get("Normalized Name") or "").strip() or normalize_name(row.get("Product Name"))
        srp_raw = (row.get("SRP") or "").strip()
        currency = (row.get("currency") or "").strip()
        start_month = (row.get("start_month") or "").strip()
        end_month = (row.get("end_month") or "").strip()
        product = (row.get("Product Name") or "").strip()

        if not slug:
            print(f"  skip {product!r}: no normalized name", file=sys.stderr)
            skipped += 1
            continue
        sku_id = name_to_sku.get(slug)
        if not sku_id:
            print(f"  skip {product!r} ({slug}): no matching SKU in catalog", file=sys.stderr)
            skipped += 1
            continue
        if not (srp_raw and currency and start_month):
            print(f"  skip {product!r}: missing SRP/currency/start_month", file=sys.stderr)
            skipped += 1
            continue

        start_date = month_start(start_month)
        end_date = month_end(end_month) if end_month else None
        body: dict = {
            "prices": [{"currency": currency, "price": float(srp_raw)}],
            "startDate": start_date,
        }
        if end_date:
            body["endDate"] = end_date

        prefix = f"{slug} ({sku_id}) {currency}={srp_raw} {start_date}->{end_date or 'open'}"
        if args.dry_run:
            print(f"  [dry-run] POST {prefix}", file=sys.stderr)
            continue

        url = f"https://{args.host}/api/v1/supplier/{args.org_id}/catalog/{sku_id}/srp"
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
