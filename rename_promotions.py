"""Set ``name`` on every LootVault promotion campaign (server-driven, no CSV).

Bucket model (playasia/lootvault#1541)
--------------------------------------
A promotion is now a single **campaign** entity identified by its time frame +
reseller scope, with **per-SKU discounts** — there is no one campaign-wide
discount. The supplier list endpoint returns one row per campaign, exposing
``discountMin``/``discountMax`` across the campaign's SKUs. Renaming is therefore
a single ``PUT`` per campaign against its ``id`` — no per-SKU line resolution.

  * ``GET  /api/v1/orgs/{org_id}/promotions`` — one row per campaign (id, dates,
    ``discountMin``/``discountMax``, resellers, ``skuIds``, ``skuCount``).
  * ``PUT  /api/v1/orgs/{org_id}/promotions/{promotion_id}`` — update the header;
    dates + reseller scope are **echoed back unchanged** so only the name moves
    (a real date/scope change is frozen once the campaign has reported sales).

Naming (matches level_2_build_promotion_campaigns.py)
-----------------------------------------------------
``"{start_date} → {end_date} {scope} {pct-or-range}%"`` — e.g.
``"2025-08-01 → 2025-08-14 Heybox 20%"``. The discount is one value when every
SKU shares it (``discountMin == discountMax``), else the range
``"{min}–{max}%"``. When a campaign covers exactly ONE SKU, a truncated product
suffix is appended (≤30 chars, trademark + " Edition" stripped):
``"2025-08-01 → 2025-08-14 Heybox 20% — ELDEN RING NIGHTREIGN"``.

``scope`` comes from the campaign's ``resellers`` array (server returns
``[{id, name}, ...]``): single reseller → that reseller's display name;
missing/empty → ``"All"``; multiple → names joined with ``+`` (rare).

Idempotent: campaigns whose name already matches the canonical form are skipped.
The header ``discountPercentage`` (UI-convenience metadata, never read for
pricing) is left out of the PUT — the bandai campaigns carry no header discount.

Note: the public ``api.yaml`` omits ``name`` from ``UpdatePromotionRequest`` but
the server accepts it.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import requests

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
CATALOG_PAGE_SIZE = 1000
PROMO_LIST_PAGE_SIZE = 500
REQUEST_DELAY_S = 0.05
TIMEOUT_S = 30
ALL_CUSTOMERS_TOKEN = "All"
PRODUCT_NAME_MAX = 30

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


def fmt_num(x: float | str, nd: int = 4) -> str:
    """Trim a number for display: integer when whole, else rounded to nd places.

    Mirrors ``fmt_num`` in level_2_build_promotion_campaigns.py so the renamer and
    the campaign builder produce byte-identical names.
    """
    v = round(float(x), nd)
    return str(int(v)) if v == int(v) else str(v)


def discount_annotation(disc_min: float | str | None, disc_max: float | str | None) -> str:
    """``"{pct}%"`` when min == max, else ``"{min}–{max}%"`` (en dash, as in level_2)."""
    lo = fmt_num(disc_min if disc_min is not None else 0)
    hi = fmt_num(disc_max if disc_max is not None else 0)
    return f"{lo}%" if lo == hi else f"{lo}–{hi}%"


def customer_label(resellers: list[dict] | None) -> str:
    if not resellers:
        return ALL_CUSTOMERS_TOKEN
    names = [r.get("name", "") for r in resellers if r.get("name")]
    if len(names) == 1:
        return names[0]
    return "+".join(sorted(names)) if names else ALL_CUSTOMERS_TOKEN


def build_name(item: dict, product: str) -> str:
    """Canonical campaign name from a promotion-list row (bucket model)."""
    scope = customer_label(item.get("resellers"))
    disc = discount_annotation(item.get("discountMin"), item.get("discountMax"))
    name = f"{item.get('startDate', '')} → {item.get('endDate', '')} {scope} {disc}"
    if int(item.get("skuCount", 0) or 0) <= 1:
        name = f"{name} — {short_product(product)}"
    return name


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


def fetch_promotions(
    session: requests.Session, host: str, org_id: str, headers: dict, status: str
) -> list[dict]:
    """Page the supplier promotion list. ``status``: '' (all), active, future, ended."""
    base = f"https://{host}/api/v1/orgs/{org_id}/promotions"
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


def put_rename(
    session: requests.Session, host: str, org_id: str, headers: dict, item: dict, new_name: str
) -> tuple[bool, str]:
    """PUT the header with the new name; dates + reseller scope echoed unchanged."""
    reseller_ids = [r["id"] for r in (item.get("resellers") or []) if r.get("id")] or None
    body = {
        "name": new_name,
        "startDate": item["startDate"],
        "endDate": item["endDate"],
        "resellers": reseller_ids,
    }
    url = f"https://{host}/api/v1/orgs/{org_id}/promotions/{item['id']}"
    resp = session.put(url, json=body, headers=headers, timeout=TIMEOUT_S)
    return resp.ok, "" if resp.ok else f"{resp.status_code} {resp.text}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID)
    parser.add_argument("--token", required=True, help="Bearer JWT")
    parser.add_argument(
        "--status",
        default="",
        help="Comma-separated statuses to process: active, future, ended, or empty "
        "for all (default: all — the bandai campaigns are backdated/ended).",
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

    statuses = [s.strip() for s in args.status.split(",")] if args.status.strip() else [""]
    seen: set[str] = set()
    items: list[dict] = []
    for status in statuses:
        label = status or "all"
        print(f"Fetching promotions (status={label})...", file=sys.stderr)
        batch = fetch_promotions(session, args.host, args.org_id, headers, status)
        fresh = [it for it in batch if it.get("id") and it["id"] not in seen]
        seen.update(it["id"] for it in fresh)
        items.extend(fresh)
        print(f"  {len(batch)} rows ({len(fresh)} new)", file=sys.stderr)

    ok = fail = skipped = unchanged = 0
    for item in items:
        promo_id = item.get("id", "")
        sku_ids = item.get("skuIds") or []
        if not sku_ids:
            print(f"  skip {promo_id}: campaign has no SKUs", file=sys.stderr)
            skipped += 1
            continue

        # Product name for single-SKU campaigns (only used when skuCount <= 1).
        product = sku_names.get(sku_ids[0], sku_ids[0])
        new_name = build_name(item, product)

        scope = customer_label(item.get("resellers"))
        disc = discount_annotation(item.get("discountMin"), item.get("discountMax"))
        prefix = (
            f"{promo_id} {item.get('startDate')}->{item.get('endDate')} "
            f"[{scope} {disc} ×{item.get('skuCount', len(sku_ids))}]"
        )

        if item.get("name", "") == new_name:
            unchanged += 1
            continue

        if args.dry_run:
            print(f"  [dry-run] PUT {prefix}: {item.get('name')!r} -> {new_name!r}", file=sys.stderr)
            continue

        success, err = put_rename(session, args.host, args.org_id, headers, item, new_name)
        if success:
            print(f"  ok   {prefix}: -> {new_name!r}", file=sys.stderr)
            ok += 1
        else:
            print(f"  FAIL {prefix} -> {err}", file=sys.stderr)
            fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(
        f"\nDone. ok={ok} unchanged={unchanged} fail={fail} skipped={skipped} "
        f"campaigns={len(items)}",
        file=sys.stderr,
    )
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
