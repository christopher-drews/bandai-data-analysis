"""Dump all Playasia Bandai digital-game products to CSV.

Walks GET /api/v1/products/bandai-digital-games with pagination and writes
paxCode + label rows to bandai_products.csv.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://play-asia.com/api/v1/products/bandai-digital-games"
PAGE_SIZE = 500  # server caps at 500
REQUEST_DELAY_S = 0.2
TIMEOUT_S = 30


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "playasia-auth/1.0"})
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="bandai_products.csv", type=Path)
    args = parser.parse_args()

    session = build_session()
    all_rows: list[dict] = []
    offset = 0
    total = None

    while True:
        resp = session.get(
            API_URL,
            params={"limit": PAGE_SIZE, "offset": offset},
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results") or []
        total = int(payload.get("totalCount") or 0)
        all_rows.extend(results)
        print(f"  offset={offset} -> {len(results)} rows (running total {len(all_rows)}/{total})", file=sys.stderr)
        if not results or len(all_rows) >= total:
            break
        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY_S)

    fieldnames = ["paxCode", "label"]
    with args.output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nWrote {args.output} ({len(all_rows)} rows; server totalCount={total})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
