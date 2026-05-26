"""Set ``name`` on existing LootVault promotions using a hybrid campaign / per-SKU label.

Walks the same CSV as ``upload_promotions.py``
(``data/level_1_extract_promo_history/product_promo_history.csv``), locates
the matching promotion on each SKU by ``startDate``/``endDate``/
``discountPercentage``/``resellers``, and PUTs the promotion back with a
short, deterministic ``name``.

Naming
------
Base label: ``"{YYYY-MM} {Customer} {pct}%"`` — e.g. ``"2025-08 Heybox 20%"``.
The supplier UI's promotion-list view groups rows sharing
``(name, start_date, end_date, discount_percentage)`` into one campaign row,
so SKUs that share the tuple collapse correctly.

When the (start_date, end_date, Customer, discount) tuple covers only ONE
SKU in this CSV, the label gets a truncated product suffix (≤30 chars,
trademark + " Edition" suffix stripped) so singletons remain identifiable:
``"2025-08 Heybox 20% — ELDEN RING NIGHTREIGN"``.

Idempotent: rows whose promotion already has the target name are skipped.

Note: the public ``api.yaml`` omits ``name`` from ``UpdatePromotionRequest``
but the server accepts it (the column exists and the Rust struct deserializes
it).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests

from upload_promotions import (
    ALL_CUSTOMERS_TOKEN,
    REQUEST_DELAY_S,
    TIMEOUT_S,
    fetch_catalog,
)
from normalize import normalize_name

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
DEFAULT_CSV = Path("data/level_1_extract_promo_history/product_promo_history.csv")
PROMOTIONS_PAGE_LIMIT = 1000
DISCOUNT_TOLERANCE = 1e-4
PRODUCT_NAME_MAX = 30
EDITION_SUFFIX_RE = re.compile(
    r"\s+(Digital\s+|Premium\s+|Ultimate\s+|Deluxe\s+|Standard\s+|Collector'?s?\s+)?Edition\s*$",
    re.IGNORECASE,
)
TRADEMARK_RE = re.compile(r"[™®]")  # ™ ®


def short_product(name: str) -> str:
    """Strip trademarks and trailing 'Edition' clause, then truncate to PRODUCT_NAME_MAX."""
    cleaned = TRADEMARK_RE.sub("", name).strip()
    cleaned = EDITION_SUFFIX_RE.sub("", cleaned).strip()
    if len(cleaned) > PRODUCT_NAME_MAX:
        cleaned = cleaned[: PRODUCT_NAME_MAX - 1].rstrip() + "…"
    return cleaned


def build_name(
    start_date: str,
    customer: str,
    discount_percentage: float,
    cluster_size: int,
    product: str,
) -> str:
    """Build the promotion name from row fields + cluster size from the pre-pass."""
    pct = f"{discount_percentage:g}"
    base = f"{start_date[:7]} {customer or 'All'} {pct}%"
    if cluster_size <= 1:
        return f"{base} — {short_product(product)}"
    return base


def fetch_sku_promotions(
    session: requests.Session, host: str, org_id: str, sku_id: str, headers: dict
) -> list[dict]:
    url = f"https://{host}/api/v1/supplier/{org_id}/catalog/{sku_id}/promotions"
    resp = session.get(
        url,
        params={"offset": 0, "limit": PROMOTIONS_PAGE_LIMIT},
        headers=headers,
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def reseller_ids(promo: dict) -> frozenset[str] | None:
    """Normalize a promotion's resellers field to a frozenset of ids, or None for all-resellers."""
    raw = promo.get("resellers")
    if not raw:
        return None
    ids = {r["id"] for r in raw if isinstance(r, dict) and r.get("id")}
    return frozenset(ids) if ids else None


def find_match(
    promos: list[dict],
    start_date: str,
    end_date: str,
    discount: float,
    target_resellers: frozenset[str] | None,
) -> list[dict]:
    matches = []
    for p in promos:
        if p.get("startDate") != start_date or p.get("endDate") != end_date:
            continue
        try:
            p_disc = float(p.get("discountPercentage"))
        except (TypeError, ValueError):
            continue
        if abs(p_disc - discount) > DISCOUNT_TOLERANCE:
            continue
        if reseller_ids(p) != target_resellers:
            continue
        matches.append(p)
    return matches


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

    # Pre-pass: count how many CSV rows share each (start, end, Customer, discount).
    # Matches the tuple the supplier UI groups campaigns on. Used to decide whether
    # a row gets the bare campaign label or the singleton (label + product) label.
    cluster_size: Counter[tuple[str, str, str, float]] = Counter()
    for row in rows:
        try:
            disc = round(float((row.get("Promo Discount") or "").strip()) * 100, 4)
        except ValueError:
            continue
        cluster_size[(
            (row.get("start_date") or "").strip(),
            (row.get("end_date") or "").strip(),
            (row.get("Customer") or "").strip() or ALL_CUSTOMERS_TOKEN,
            disc,
        )] += 1

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    session = requests.Session()

    print("Fetching supplier catalog...", file=sys.stderr)
    catalog = fetch_catalog(session, args.host, args.org_id, headers)
    print(f"  {len(catalog)} SKUs indexed by normalized name", file=sys.stderr)

    sku_promo_cache: dict[str, list[dict]] = {}
    ok = fail = skipped = unchanged = 0

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
            discount_percentage = round(float(promo_raw) * 100, 4)
        except ValueError:
            print(f"  skip {product!r}: bad Promo Discount value {promo_raw!r}", file=sys.stderr)
            skipped += 1
            continue

        target_resellers: frozenset[str] | None
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
            target_resellers = frozenset({reseller_id})
        else:
            target_resellers = None

        if sku_id not in sku_promo_cache:
            sku_promo_cache[sku_id] = fetch_sku_promotions(
                session, args.host, args.org_id, sku_id, headers
            )
        promos = sku_promo_cache[sku_id]

        matches = find_match(promos, start_date, end_date, discount_percentage, target_resellers)
        scope_desc = "all" if target_resellers is None else ",".join(sorted(target_resellers))
        prefix = (
            f"{slug} ({sku_id}) {discount_percentage}% "
            f"{start_date}->{end_date} [{customer or 'All'}={scope_desc}]"
        )

        if not matches:
            print(f"  WARN no matching promotion for {prefix}", file=sys.stderr)
            skipped += 1
            continue
        if len(matches) > 1:
            ids = [m.get("id") for m in matches]
            print(f"  WARN multiple matches for {prefix}: {ids}", file=sys.stderr)
            skipped += 1
            continue

        promo = matches[0]
        cluster_key = (start_date, end_date, customer or ALL_CUSTOMERS_TOKEN, discount_percentage)
        new_name = build_name(
            start_date,
            customer,
            discount_percentage,
            cluster_size[cluster_key],
            product,
        )
        if promo.get("name") == new_name:
            unchanged += 1
            continue

        body = {
            "discountPercentage": promo["discountPercentage"],
            "startDate": promo["startDate"],
            "endDate": promo["endDate"],
            "currencies": promo.get("currencies"),
            "resellers": [r["id"] for r in (promo.get("resellers") or [])] or None,
            "name": new_name,
        }

        if args.dry_run:
            print(f"  [dry-run] PUT {prefix} name={new_name!r} (was {promo.get('name')!r})", file=sys.stderr)
            continue

        url = (
            f"https://{args.host}/api/v1/supplier/{args.org_id}"
            f"/catalog/{sku_id}/promotions/{promo['id']}"
        )
        resp = session.put(url, json=body, headers=headers, timeout=TIMEOUT_S)
        if resp.ok:
            print(f"  ok   {prefix} -> {new_name!r}", file=sys.stderr)
            ok += 1
        else:
            print(f"  FAIL {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(
        f"\nDone. ok={ok} unchanged={unchanged} fail={fail} skipped={skipped} total={len(rows)}",
        file=sys.stderr,
    )
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
