"""Set ``name`` on every active/future LootVault promotion (server-driven, no CSV).

Pages the supplier's ``/api/v1/supplier/{org_id}/promotion-lists`` endpoint,
which returns every promotion grouped by ``COALESCE(name, id)``. For
currently-unnamed promotions the ``groupKey`` is the promotion id, so we can
PUT directly without per-SKU listing. For already-named groups we only
intervene if the canonical name we compute differs from the existing one
(rare during normal operation).

Naming
------
Base label: ``"{YYYY-MM} {Customer} {pct}%"`` — e.g. ``"2025-08 Heybox 20%"``.
When multiple promotions share ``(start_date, end_date, resellers, discount)``,
they collapse into one campaign row in the supplier UI.

When the tuple covers only ONE promotion server-wide, the label gets a
truncated product suffix (≤30 chars, trademark + " Edition" stripped):
``"2025-08 Heybox 20% — ELDEN RING NIGHTREIGN"``.

The "customer" comes directly from the promotion's ``resellers`` array
(server returns ``[{id, name}, ...]``): single reseller → that reseller's
display name; missing/empty → ``"All"``; multiple → reseller names joined
with ``+`` (rare).

Idempotent: rows whose name already matches the canonical form are skipped.

Note: the public ``api.yaml`` omits ``name`` from ``UpdatePromotionRequest``
but the server accepts it.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter

import requests

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
CATALOG_PAGE_SIZE = 1000
PROMO_LIST_PAGE_SIZE = 500
PROMOTIONS_PAGE_LIMIT = 1000
REQUEST_DELAY_S = 0.05
TIMEOUT_S = 30
ALL_CUSTOMERS_TOKEN = "All"
PRODUCT_NAME_MAX = 30
DISCOUNT_TOLERANCE = 1e-4

EDITION_SUFFIX_RE = re.compile(
    r"\s+(Digital\s+|Premium\s+|Ultimate\s+|Deluxe\s+|Standard\s+|Collector'?s?\s+)?Edition\s*$",
    re.IGNORECASE,
)
TRADEMARK_RE = re.compile(r"[™®]")


def short_product(name: str) -> str:
    """Strip trademarks and trailing 'Edition' clause, then truncate to PRODUCT_NAME_MAX."""
    cleaned = TRADEMARK_RE.sub("", name or "").strip()
    cleaned = EDITION_SUFFIX_RE.sub("", cleaned).strip()
    if len(cleaned) > PRODUCT_NAME_MAX:
        cleaned = cleaned[: PRODUCT_NAME_MAX - 1].rstrip() + "…"
    return cleaned


def build_name(
    start_date: str, customer: str, discount_percentage: float, cluster_size: int, product: str
) -> str:
    pct = f"{discount_percentage:g}"
    base = f"{start_date[:7]} {customer or ALL_CUSTOMERS_TOKEN} {pct}%"
    if cluster_size <= 1:
        return f"{base} — {short_product(product)}"
    return base


def customer_label(resellers: list[dict] | None) -> str:
    if not resellers:
        return ALL_CUSTOMERS_TOKEN
    names = [r.get("name", "") for r in resellers if r.get("name")]
    if len(names) == 1:
        return names[0]
    return "+".join(sorted(names)) if names else ALL_CUSTOMERS_TOKEN


def reseller_id_set(resellers: list[dict] | None) -> frozenset[str]:
    if not resellers:
        return frozenset()
    return frozenset(r["id"] for r in resellers if isinstance(r, dict) and r.get("id"))


def cluster_key(item: dict) -> tuple[str, str, frozenset[str], float]:
    return (
        item.get("startDate", ""),
        item.get("endDate", ""),
        reseller_id_set(item.get("resellers")),
        round(float(item.get("discountPercentage", 0)), 4),
    )


def fetch_sku_names(
    session: requests.Session, host: str, org_id: str, headers: dict
) -> dict[str, str]:
    """Return {sku_id: product_name} for every SKU in the supplier's catalog."""
    base = f"https://{host}/api/v1/lv-team/catalog"
    names: dict[str, str] = {}
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
            sku = item.get("id")
            name = item.get("name")
            if sku and name:
                names[sku] = name
        offset += len(items)
        if len(items) < CATALOG_PAGE_SIZE or offset >= payload.get("total", offset):
            break
    return names


def fetch_promotion_groups(
    session: requests.Session, host: str, org_id: str, headers: dict, status: str
) -> list[dict]:
    base = f"https://{host}/api/v1/supplier/{org_id}/promotion-lists"
    items: list[dict] = []
    offset = 0
    while True:
        resp = session.get(
            base,
            params={"status": status, "offset": offset, "limit": PROMO_LIST_PAGE_SIZE},
            headers=headers,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("items", [])
        items.extend(batch)
        offset += len(batch)
        if len(batch) < PROMO_LIST_PAGE_SIZE or offset >= payload.get("total", offset):
            break
    return items


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


def resolve_group_promo_ids(
    session: requests.Session,
    host: str,
    org_id: str,
    headers: dict,
    item: dict,
) -> list[tuple[str, str]]:
    """For a /promotion-lists group, return [(sku_id, promo_id), ...] by per-SKU GET.

    Used when groupKey is a name (not a promo id), so we need individual
    promotion ids to issue PUTs. Matches by dates + discount + resellers.
    """
    target_dates = (item.get("startDate"), item.get("endDate"))
    target_disc = round(float(item.get("discountPercentage", 0)), 4)
    target_resellers = reseller_id_set(item.get("resellers"))
    out: list[tuple[str, str]] = []
    for sku_id in item.get("skuIds", []):
        for promo in fetch_sku_promotions(session, host, org_id, sku_id, headers):
            if (promo.get("startDate"), promo.get("endDate")) != target_dates:
                continue
            try:
                if abs(float(promo.get("discountPercentage")) - target_disc) > DISCOUNT_TOLERANCE:
                    continue
            except (TypeError, ValueError):
                continue
            if reseller_id_set(promo.get("resellers")) != target_resellers:
                continue
            out.append((sku_id, promo["id"]))
    return out


def put_rename(
    session: requests.Session,
    host: str,
    org_id: str,
    headers: dict,
    sku_id: str,
    promo_id: str,
    item: dict,
    new_name: str,
) -> tuple[bool, str]:
    body = {
        "discountPercentage": item["discountPercentage"],
        "startDate": item["startDate"],
        "endDate": item["endDate"],
        "currencies": item.get("currencies"),
        "resellers": [r["id"] for r in (item.get("resellers") or [])] or None,
        "name": new_name,
    }
    url = f"https://{host}/api/v1/supplier/{org_id}/catalog/{sku_id}/promotions/{promo_id}"
    resp = session.put(url, json=body, headers=headers, timeout=TIMEOUT_S)
    return resp.ok, f"{resp.status_code} {resp.text}" if not resp.ok else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID)
    parser.add_argument("--token", required=True, help="Bearer JWT")
    parser.add_argument(
        "--status",
        default="active,future",
        help="Comma-separated statuses to process (default: active,future)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    session = requests.Session()

    print("Fetching SKU catalog (sku_id -> name)...", file=sys.stderr)
    sku_names = fetch_sku_names(session, args.host, args.org_id, headers)
    print(f"  {len(sku_names)} SKUs", file=sys.stderr)

    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    items: list[dict] = []
    for status in statuses:
        print(f"Fetching promotion groups (status={status})...", file=sys.stderr)
        batch = fetch_promotion_groups(session, args.host, args.org_id, headers, status)
        print(f"  {len(batch)} groups", file=sys.stderr)
        items.extend(batch)

    # Natural cluster size: count actual promotions (sku_count per group), not groups.
    # Two unnamed promos that share (dates, resellers, discount) appear as two
    # groups in the response — but they should collapse into one campaign once
    # we give them the same name.
    cluster_size: Counter[tuple[str, str, frozenset[str], float]] = Counter()
    for it in items:
        cluster_size[cluster_key(it)] += int(it.get("skuCount", 1) or 1)

    ok = fail = skipped = unchanged = 0
    for item in items:
        group_key = item.get("groupKey", "")
        start_date = item.get("startDate", "")
        discount = round(float(item.get("discountPercentage", 0)), 4)
        customer = customer_label(item.get("resellers"))
        size = cluster_size[cluster_key(item)]
        sku_ids = item.get("skuIds") or []

        if not sku_ids:
            print(f"  skip {group_key}: no skuIds in group", file=sys.stderr)
            skipped += 1
            continue

        # Product for singleton naming: pick the first SKU's name (only matters when size==1).
        first_sku = sku_ids[0]
        product = sku_names.get(first_sku, first_sku)
        new_name = build_name(start_date, customer, discount, size, product)

        is_unnamed = group_key.startswith("promo-")
        scope = customer if customer == ALL_CUSTOMERS_TOKEN else customer
        prefix = (
            f"{first_sku} {discount}% {start_date}->{item.get('endDate')} "
            f"[{scope} ×{size}]"
        )

        if not is_unnamed:
            # groupKey is the current name shared by all promos in the group.
            if group_key == new_name:
                unchanged += int(item.get("skuCount", 1) or 1)
                continue
            # Name differs — need to fall back to per-SKU promotion listing to
            # get individual promo ids, then PUT each.
            print(
                f"  RENAME-group {prefix}: {group_key!r} -> {new_name!r}",
                file=sys.stderr,
            )
            pairs = resolve_group_promo_ids(session, args.host, args.org_id, headers, item)
            if not pairs:
                print(f"    WARN could not resolve any promo ids", file=sys.stderr)
                skipped += 1
                continue
            for sku_id, promo_id in pairs:
                if args.dry_run:
                    print(f"    [dry-run] PUT {sku_id}/{promo_id}", file=sys.stderr)
                    continue
                success, err = put_rename(
                    session, args.host, args.org_id, headers, sku_id, promo_id, item, new_name
                )
                if success:
                    ok += 1
                else:
                    print(f"    FAIL {sku_id}/{promo_id} -> {err}", file=sys.stderr)
                    fail += 1
                time.sleep(REQUEST_DELAY_S)
            continue

        # Unnamed: groupKey IS the promotion id. Single promo in this group.
        promo_id = group_key
        sku_id = first_sku
        if args.dry_run:
            print(f"  [dry-run] PUT {sku_id}/{promo_id} {prefix} -> {new_name!r}", file=sys.stderr)
            continue

        success, err = put_rename(
            session, args.host, args.org_id, headers, sku_id, promo_id, item, new_name
        )
        if success:
            print(f"  ok   {sku_id}/{promo_id} {prefix} -> {new_name!r}", file=sys.stderr)
            ok += 1
        else:
            print(f"  FAIL {sku_id}/{promo_id} {prefix} -> {err}", file=sys.stderr)
            fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(
        f"\nDone. ok={ok} unchanged={unchanged} fail={fail} skipped={skipped} groups={len(items)}",
        file=sys.stderr,
    )
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
