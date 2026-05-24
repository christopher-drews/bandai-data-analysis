"""Report level_2 sales history to LootVault month-by-month.

For every (reseller, month) pair in data/level_2_anonymize_sales_history/
product_sales_history.csv, this script:

  1. Resolves paxCode -> sku_id via /api/v1/lv-team/catalog.
  2. Resolves Customer Reference -> reseller org_id via the customer/org map.
  3. Pulls unsold keys the reseller owns for that SKU via
       GET /api/v1/orgs/{reseller_org_id}/inventory/{sku_id}/keys?status=unsold
  4. Assigns ``amount`` keyIds per CSV row, each with a random ISO 8601
     timestamp uniformly distributed in [start_of(start_month),
     end_of(end_month) + 1 day).
  5. Submits one batched report per (month, reseller) to
       POST /api/v1/reseller/{reseller_org_id}/reports/json

Run prepare_sales_upload.py first — it stages enough unsold keys per
reseller for this script to consume.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import random
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from prepare_sales_upload import (
    DEFAULT_CUSTOMER_MAP,
    DEFAULT_HOST,
    DEFAULT_ORG_ID,
    REQUEST_DELAY_S,
    TIMEOUT_S,
    amount_as_int,
    fetch_catalog_by_paxcode,
    load_customer_org_map,
)

DEFAULT_CSV = Path("data/level_2_anonymize_sales_history/product_sales_history.csv")
KEYS_PAGE_SIZE = 1000


def parse_month(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m").replace(tzinfo=UTC)


def month_window(start_month: str, end_month: str) -> tuple[datetime, datetime]:
    """Inclusive window from the first instant of start_month to the last
    instant before end_month + 1 month."""
    start = parse_month(start_month)
    end = parse_month(end_month)
    last_day = calendar.monthrange(end.year, end.month)[1]
    end_exclusive = end.replace(day=last_day) + timedelta(days=1)
    return start, end_exclusive


def random_timestamps(rng: random.Random, start: datetime, end_exclusive: datetime, n: int) -> list[str]:
    span = int((end_exclusive - start).total_seconds())
    if span <= 0:
        return [start.strftime("%Y-%m-%dT%H:%M:%SZ") for _ in range(n)]
    out: list[str] = []
    for _ in range(n):
        offset = rng.randrange(span)
        ts = start + timedelta(seconds=offset)
        out.append(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return out


def fetch_reseller_keys(
    session: requests.Session,
    host: str,
    reseller_org: str,
    sku_id: str,
    headers: dict,
    needed: int,
) -> list[str]:
    """Page through unsold reseller keys until we have at least ``needed`` IDs."""
    base = f"https://{host}/api/v1/orgs/{reseller_org}/inventory/{sku_id}/keys"
    ids: list[str] = []
    offset = 0
    while len(ids) < needed:
        resp = session.get(
            base,
            params={
                "status": "unsold",
                "offset": offset,
                "limit": KEYS_PAGE_SIZE,
                "placement": "reseller",
            },
            headers=headers,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        if not items:
            break
        for item in items:
            kid = item.get("id") or item.get("keyId")
            if kid:
                ids.append(kid)
        offset += len(items)
        total = payload.get("total")
        if total is not None and offset >= total:
            break
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV, type=Path)
    parser.add_argument("--customer-org-map", default=DEFAULT_CUSTOMER_MAP, type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID,
                        help="Supplier organisation id (used only for catalog lookup)")
    parser.add_argument("--token", required=True, help="Bearer JWT")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for timestamp generation")
    parser.add_argument("--month", default=None, help="Optional YYYY-MM filter; defaults to all months")
    args = parser.parse_args()

    with args.csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    cust_to_org = load_customer_org_map(args.customer_org_map)
    if not cust_to_org:
        print(
            f"error: {args.customer_org_map} has no usable rows "
            "(populate the customer_reference column before running)",
            file=sys.stderr,
        )
        return 1
    print(f"Loaded {len(cust_to_org)} customer->org mappings", file=sys.stderr)

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    session = requests.Session()

    print("Fetching supplier catalog...", file=sys.stderr)
    pax_to_sku = fetch_catalog_by_paxcode(session, args.host, args.org_id, headers)
    print(f"  {len(pax_to_sku)} SKUs indexed by paxCode", file=sys.stderr)

    rng = random.Random(args.seed)

    # Group: month -> reseller_org -> sku_id -> list of (count, price, currency, start_month, end_month)
    grouped: dict[str, dict[str, dict[str, list[dict]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    skipped_pax = skipped_cust = skipped_amount = skipped_filter = 0
    for row in rows:
        start_month = (row.get("start_month") or "").strip()
        if args.month and start_month != args.month:
            skipped_filter += 1
            continue
        amount = amount_as_int(row.get("amount", ""))
        if amount <= 0:
            skipped_amount += 1
            continue
        pax = (row.get("paxCode") or "").strip()
        cust = (row.get("Customer Reference") or "").strip()
        sku_id = pax_to_sku.get(pax)
        if not sku_id:
            skipped_pax += 1
            continue
        reseller_org = cust_to_org.get(cust)
        if not reseller_org:
            skipped_cust += 1
            continue
        try:
            price = float(row.get("selling_price") or 0.0)
        except ValueError:
            price = 0.0
        currency = (row.get("currency") or "").strip() or "CNY"
        end_month = (row.get("end_month") or start_month).strip()
        grouped[start_month][reseller_org][sku_id].append({
            "count": amount,
            "price": round(price, 2),
            "currency": currency,
            "start_month": start_month,
            "end_month": end_month,
        })

    print(
        f"Grouped {sum(len(per_org) for per_month in grouped.values() for per_org in per_month.values())} "
        f"(month, reseller, sku) groups across {len(grouped)} months. "
        f"Skipped: pax={skipped_pax} customer={skipped_cust} amount={skipped_amount} month-filter={skipped_filter}",
        file=sys.stderr,
    )

    report_ok = report_fail = 0
    for month in sorted(grouped):
        for reseller_org in sorted(grouped[month]):
            entries: list[dict] = []
            short_skus: list[str] = []
            for sku_id in sorted(grouped[month][reseller_org]):
                row_specs = grouped[month][reseller_org][sku_id]
                keys_needed = sum(spec["count"] for spec in row_specs)

                if args.dry_run:
                    keyids = [f"<dryrun-key-{sku_id}-{i}>" for i in range(keys_needed)]
                else:
                    keyids = fetch_reseller_keys(
                        session, args.host, reseller_org, sku_id, headers, keys_needed
                    )
                if len(keyids) < keys_needed:
                    short_skus.append(f"{sku_id} (need {keys_needed}, have {len(keyids)})")
                    continue

                cursor = 0
                for spec in row_specs:
                    n = spec["count"]
                    window_start, window_end = month_window(spec["start_month"], spec["end_month"])
                    timestamps = random_timestamps(rng, window_start, window_end, n)
                    for keyid, ts in zip(keyids[cursor:cursor + n], timestamps, strict=True):
                        entries.append({
                            "skuId": sku_id,
                            "keyId": keyid,
                            "date": ts,
                            "price": spec["price"],
                            "currency": spec["currency"],
                        })
                    cursor += n

            if short_skus:
                print(
                    f"  FAIL {month} reseller={reseller_org}: insufficient unsold keys for "
                    + ", ".join(short_skus),
                    file=sys.stderr,
                )
                report_fail += 1
                continue
            if not entries:
                continue

            prefix = f"{month} reseller={reseller_org}: {len(entries)} entries"
            if args.dry_run:
                print(f"  [dry-run] POST report {prefix}", file=sys.stderr)
                continue

            url = f"https://{args.host}/api/v1/reseller/{reseller_org}/reports/json"
            resp = session.post(url, json={"entries": entries}, headers=headers, timeout=TIMEOUT_S)
            if resp.ok:
                print(f"  ok   report {prefix}", file=sys.stderr)
                report_ok += 1
            else:
                print(f"  FAIL report {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
                report_fail += 1
            time.sleep(REQUEST_DELAY_S)

    print(f"\nDone. reports ok={report_ok} fail={report_fail}", file=sys.stderr)
    return 1 if report_fail else 0


if __name__ == "__main__":
    sys.exit(main())
