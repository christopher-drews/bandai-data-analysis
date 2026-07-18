"""Dump all Playasia Bandai digital-game products to CSV (offline catalog).

Walks GET /api/v1/products/bandai-digital-games with pagination and writes one
row per product to data/bandai_catalog.csv, capturing paxCode, label, and
steamId (plus any other fields the API returns). This is the offline source for
associating royalty SKUs to paxCodes — fetch it once here, then match locally
(level_2_enrich_pax_codes.py) rather than calling the API per SKU.

The endpoint is fronted by Cloudflare; the allowlisted ``playasia-auth/1.0``
User-Agent (set in build_session) is what gets a 200 instead of a bot challenge.
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


# Preferred column order; any other keys the API returns are appended after these.
PREFERRED_COLUMNS = ["paxCode", "label", "steamId"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=Path("data/bandai_catalog.csv"), type=Path)
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

    # Union of all keys seen, preferred columns first — so a new API field is
    # captured for later use instead of silently dropped.
    extra = sorted({k for row in all_rows for k in row} - set(PREFERRED_COLUMNS))
    fieldnames = [c for c in PREFERRED_COLUMNS if any(c in r for r in all_rows)] + extra

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    with_steam = sum(1 for r in all_rows if r.get("steamId") not in (None, ""))
    print(f"\nWrote {args.output} ({len(all_rows)} rows; server totalCount={total})", file=sys.stderr)
    print(f"  columns: {fieldnames}", file=sys.stderr)
    print(f"  with steamId: {with_steam}/{len(all_rows)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
