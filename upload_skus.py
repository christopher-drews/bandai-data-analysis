"""Create Bandai SKUs in a LootVault supplier catalog from data/bandai_products.csv.

For each row, POSTs to
    /api/v1/supplier/{org_id}/catalog
with a CreateSkuRequest body ``{name, supplier:[org_id], paPaxCode}``.

This is the seeding step the downstream uploads (upload_srps, upload_promotions,
prepare_sales_upload, upload_sales_history) all depend on: they resolve a SKU by
its ``paPaxCode``, so the catalog must contain these items first.

Re-runs are idempotent: existing catalog items (matched on ``paPaxCode``) are
skipped, so it is safe to re-run after a partial failure.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import requests

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
DEFAULT_CSV = Path("data/bandai_products.csv")
CATALOG_PAGE_SIZE = 1000
REQUEST_DELAY_S = 0.1
TIMEOUT_S = 30


def fetch_existing_pax_codes(session: requests.Session, host: str, org_id: str, headers: dict) -> set[str]:
    """Return the set of ``paPaxCode`` values already present for this supplier.

    Uses /api/v1/lv-team/catalog filtered to the supplier, mirroring the lookup
    the other upload scripts use.
    """
    base = f"https://{host}/api/v1/lv-team/catalog"
    seen: set[str] = set()
    offset = 0
    while True:
        resp = session.get(
            base,
            params={"supplier": org_id, "offset": offset, "limit": CATALOG_PAGE_SIZE},
            headers=headers,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        for item in items:
            pax = (item.get("paPaxCode") or "").strip()
            if pax:
                seen.add(pax)
        offset += len(items)
        if len(items) < CATALOG_PAGE_SIZE or offset >= payload.get("total", offset):
            break
    return seen


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

    print("Fetching existing supplier catalog...", file=sys.stderr)
    existing = fetch_existing_pax_codes(session, args.host, args.org_id, headers)
    print(f"  {len(existing)} SKUs already present (by paPaxCode)", file=sys.stderr)

    url = f"https://{args.host}/api/v1/supplier/{args.org_id}/catalog"
    ok = fail = skipped = 0
    for row in rows:
        pax = (row.get("paxCode") or "").strip()
        name = (row.get("label") or "").strip()

        if not pax or not name:
            print(f"  skip {name or pax!r}: missing paxCode or label", file=sys.stderr)
            skipped += 1
            continue
        if pax in existing:
            print(f"  skip {name!r} ({pax}): already in catalog", file=sys.stderr)
            skipped += 1
            continue

        body = {"name": name, "supplier": [args.org_id], "paPaxCode": pax}
        prefix = f"{name!r} ({pax})"
        if args.dry_run:
            print(f"  [dry-run] POST {prefix}", file=sys.stderr)
            continue

        resp = session.post(url, json=body, headers=headers, timeout=TIMEOUT_S)
        if resp.ok:
            print(f"  ok   {prefix}", file=sys.stderr)
            existing.add(pax)
            ok += 1
        else:
            print(f"  FAIL {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(f"\nDone. ok={ok} fail={fail} skipped={skipped} total={len(rows)}", file=sys.stderr)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
