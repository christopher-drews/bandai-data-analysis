"""Upload monthly CNY exchange rates from data/level_0_extract_exchange_rates/exchange_rates.csv into LootVault.

PUTs each (year, month) row to /api/v1/supplier/{org_id}/exchange-rates,
which is an upsert: existing rates for that month are replaced.
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
DEFAULT_CURRENCY = "CNY"
DEFAULT_CSV = Path("data/level_0_extract_exchange_rates/exchange_rates.csv")
REQUEST_DELAY_S = 0.1
TIMEOUT_S = 30


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

    url = f"https://{args.host}/api/v1/supplier/{args.org_id}/exchange-rates"
    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }

    session = requests.Session()
    ok = 0
    fail = 0

    for row in rows:
        year_str, month_str = row["month"].split("-")
        year = int(year_str)
        month = int(month_str)
        rate = float(row["exchange_rate"])

        body = {
            "year": year,
            "month": month,
            "rates": [{"currency": args.currency, "rateToUsd": rate}],
        }

        prefix = f"{year}-{month:02d} {args.currency}={rate}"
        if args.dry_run:
            print(f"  [dry-run] PUT {prefix}", file=sys.stderr)
            continue

        resp = session.put(url, json=body, headers=headers, timeout=TIMEOUT_S)
        if resp.ok:
            print(f"  ok   {prefix}", file=sys.stderr)
            ok += 1
        else:
            print(f"  FAIL {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(f"\nDone. ok={ok} fail={fail} total={len(rows)}", file=sys.stderr)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
