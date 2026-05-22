"""Resolve PAX codes for distinct_products.csv via Playasia /products/quick.

For each unique Product Name in the input CSV, query the public quick-search
endpoint, take the top result, and record the PAX code plus a fuzzy similarity
score against the CSV name. The existing Item Number column is compared to the
lookup result so mismatches surface in the output.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from rapidfuzz import fuzz
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://play-asia.com/api/v1/products/quick"
REQUEST_DELAY_S = 0.2
TIMEOUT_S = 15


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
    session.headers.update({"User-Agent": "bandai-royalty-analysis/1.0"})
    return session


def quick_search(session: requests.Session, name: str) -> dict:
    resp = session.get(
        API_URL,
        params={"q": name, "language": "en"},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def classify(csv_item: str, lookup_pax: str, total_count: int) -> str:
    if total_count == 0 or not lookup_pax:
        return "not_found"
    if str(csv_item).strip() in ("0", "", "nan"):
        return "csv_invalid"
    if str(csv_item).strip() == lookup_pax:
        return "matches"
    return "differs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="distinct_products.csv", type=Path)
    parser.add_argument("--output", default="distinct_products_with_pax.csv", type=Path)
    args = parser.parse_args()

    df = pd.read_csv(args.input, dtype=str).fillna("")
    unique_names = df["Product Name"].drop_duplicates().tolist()
    print(f"Looking up {len(unique_names)} unique product names...", file=sys.stderr)

    session = build_session()
    cache: dict[str, tuple[str, str, int]] = {}  # name -> (pax, label, totalCount)

    for i, name in enumerate(unique_names, 1):
        try:
            payload = quick_search(session, name)
            results = payload.get("results") or []
            total = int(payload.get("totalCount") or 0)
            if results:
                top = results[0]
                cache[name] = (top.get("paxCode", ""), top.get("label", ""), total)
            else:
                cache[name] = ("", "", total)
        except requests.RequestException as exc:
            print(f"  [{i}/{len(unique_names)}] ERROR for {name!r}: {exc}", file=sys.stderr)
            cache[name] = ("", "", 0)
        if i % 25 == 0:
            print(f"  ...{i}/{len(unique_names)}", file=sys.stderr)
        time.sleep(REQUEST_DELAY_S)

    df["lookup_pax_code"] = df["Product Name"].map(lambda n: cache[n][0])
    df["lookup_label"] = df["Product Name"].map(lambda n: cache[n][1])
    df["lookup_total_count"] = df["Product Name"].map(lambda n: cache[n][2])
    df["lookup_score"] = [
        round(fuzz.WRatio(name, label), 1) if label else 0.0
        for name, label in zip(df["Product Name"], df["lookup_label"])
    ]
    df["match_status"] = [
        classify(item, pax, total)
        for item, pax, total in zip(df["Item Number"], df["lookup_pax_code"], df["lookup_total_count"])
    ]

    df.to_csv(args.output, index=False)
    print(f"\nWrote {args.output} ({len(df)} rows)", file=sys.stderr)

    summary = df["match_status"].value_counts().to_dict()
    print("\nmatch_status counts:", file=sys.stderr)
    for status, count in sorted(summary.items()):
        print(f"  {status}: {count}", file=sys.stderr)
    print(f"\nmean lookup_score: {df['lookup_score'].mean():.1f}", file=sys.stderr)

    low = df.nsmallest(10, "lookup_score")[["Product Name", "lookup_label", "lookup_score", "match_status"]]
    print("\n10 lowest-confidence matches:", file=sys.stderr)
    print(low.to_string(index=False), file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
