"""Reconcile reseller inventory with the current level_2 demand.

Use this when prepare_sales_upload.py left some (reseller, sku) pairs short
of keys — typically because:
  - The sales CSV changed after prepare ran (e.g., All-rows were split into
    Heybox/Sonkwo, raising per-reseller demand).
  - prepare was interrupted before finishing some transfers.
  - Some transfers silently failed in earlier runs.

The script trusts **server state**, not the local state file, so it's safe
to run repeatedly. Flow:

  1. Compute per (reseller, sku) demand from the current CSV.
  2. For each pair, query the reseller's actual unsold inventory via
       GET /api/v1/orgs/{org_id}/inventory/{sku_id}/keys
         ?status=unsold&placement=reseller&limit=1
     and read the ``total`` field.
  3. gap = max(0, demand - actual). Skip pairs with gap == 0.
  4. For each SKU with a positive total gap:
       - Query the supplier vault's unsold count for that SKU (placement=vault).
       - Upload max(0, total_gap - supplier_unsold) new keys to the supplier.
  5. Before transferring each SKU's gaps, re-query the supplier vault and
     poll until it has at least ``total_gap`` unsold keys (uploads return a
     201 with a Job that processes asynchronously — keys aren't always
     visible the moment the POST returns).
  6. Transfer each (sku, reseller) gap from supplier to reseller.

Both uploads and transfers are appended to data/.prepare_sales_state.json
(same schema as prepare_sales_upload.py) so subsequent normal runs treat
them as done.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import requests

from pa_auth import build_session
from prepare_sales_upload import (
    DEFAULT_CSV,
    DEFAULT_CUSTOMER_MAP,
    DEFAULT_HOST,
    DEFAULT_ORG_ID,
    DEFAULT_STATE_FILE,
    REQUEST_DELAY_S,
    TIMEOUT_S,
    aggregate,
    fetch_catalog_by_paxcode,
    load_customer_org_map,
    load_state,
    post_with_retry,
    save_state,
)


SUPPLIER_POLL_ATTEMPTS = 12
SUPPLIER_POLL_DELAY_S = 5


def count_unsold_keys(
    session: requests.Session,
    host: str,
    org_id: str,
    sku_id: str,
    headers: dict,
    placement: str | None = None,
) -> int:
    """Return the count of unsold keys this org currently owns for this SKU."""
    params: dict = {"status": "unsold", "limit": 1, "offset": 0}
    if placement:
        params["placement"] = placement
    url = f"https://{host}/api/v1/orgs/{org_id}/inventory/{sku_id}/keys"
    resp = session.get(url, params=params, headers=headers, timeout=TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    total = payload.get("total")
    if total is None:
        return len(payload.get("items", []))
    return int(total)


def wait_for_supplier_unsold(
    session: requests.Session,
    host: str,
    supplier_org: str,
    sku_id: str,
    headers: dict,
    needed: int,
) -> int:
    """Poll supplier vault until it has at least ``needed`` unsold keys for this
    SKU. Returns the final observed count (may still be < needed if the upload
    job is slow or failed). Uploads are async, so this gives the server a few
    seconds to finish ingesting before we transfer.
    """
    last = 0
    for attempt in range(SUPPLIER_POLL_ATTEMPTS):
        last = count_unsold_keys(session, host, supplier_org, sku_id, headers, placement="vault")
        if last >= needed:
            return last
        if attempt < SUPPLIER_POLL_ATTEMPTS - 1:
            print(
                f"    supplier {sku_id} has {last}/{needed} unsold — waiting {SUPPLIER_POLL_DELAY_S}s",
                file=sys.stderr,
            )
            time.sleep(SUPPLIER_POLL_DELAY_S)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV, type=Path)
    parser.add_argument("--customer-org-map", default=DEFAULT_CUSTOMER_MAP, type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID, help="Supplier organisation id")
    parser.add_argument("--token", help="Bearer JWT (or use --email/--password for auto-refresh on expiry)")
    parser.add_argument("--email", help="Login email; with --password, re-authenticates when the token expires")
    parser.add_argument("--password", help="Login password")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, type=Path,
                        help="prepare_sales_upload state file to update with new uploads/transfers.")
    parser.add_argument(
        "--idempotency-prefix",
        default=f"reconcile-{datetime.now().strftime('%Y%m%d-%H%M')}-{uuid4().hex[:6]}",
        help="Used to build X-Idempotency-Key per SKU top-up upload",
    )
    args = parser.parse_args()

    with args.csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    cust_to_org = load_customer_org_map(args.customer_org_map)
    if not cust_to_org:
        print(f"error: {args.customer_org_map} has no usable rows", file=sys.stderr)
        return 1
    print(f"Loaded {len(cust_to_org)} customer->org mappings", file=sys.stderr)

    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}
    session = build_session(args.host, args.token, args.email, args.password)

    print("Fetching supplier catalog...", file=sys.stderr)
    pax_to_sku = fetch_catalog_by_paxcode(session, args.host, args.org_id, headers)
    print(f"  {len(pax_to_sku)} SKUs indexed by paxCode", file=sys.stderr)

    _, per_sku_reseller, skipped_pax, skipped_cust = aggregate(rows, pax_to_sku, cust_to_org)
    print(
        f"Demand: {len(per_sku_reseller)} (sku, reseller) pairs. "
        f"Skipped {skipped_pax} unmatched-pax, {skipped_cust} unmatched-customer rows.",
        file=sys.stderr,
    )

    print("\n== Querying current reseller inventory ==", file=sys.stderr)
    gaps: dict[tuple[str, str], int] = {}
    for (sku, org), demand in sorted(per_sku_reseller.items()):
        actual = count_unsold_keys(session, args.host, org, sku, headers, placement="reseller")
        gap = max(0, demand - actual)
        gaps[(sku, org)] = gap
        if gap > 0:
            print(f"  {sku} -> {org}: have {actual}, need {demand}, gap {gap}", file=sys.stderr)
        time.sleep(0.02)

    per_sku_gap_total: dict[str, int] = defaultdict(int)
    for (sku, _), gap in gaps.items():
        per_sku_gap_total[sku] += gap
    skus_to_topup = {sku: total for sku, total in per_sku_gap_total.items() if total > 0}

    transfer_targets = sum(1 for g in gaps.values() if g > 0)
    print(
        f"\n{len(skus_to_topup)} SKUs need top-up "
        f"({sum(skus_to_topup.values())} extra keys total); "
        f"{transfer_targets} (sku, reseller) transfers needed.",
        file=sys.stderr,
    )
    if not skus_to_topup:
        print("Nothing to reconcile.", file=sys.stderr)
        return 0

    state = load_state(args.state_file)

    print("\n== Top-up uploads ==", file=sys.stderr)
    upload_ok = upload_fail = upload_skip = 0
    for sku, total_gap in sorted(skus_to_topup.items()):
        supplier_unsold = count_unsold_keys(
            session, args.host, args.org_id, sku, headers, placement="vault"
        )
        upload_count = max(0, total_gap - supplier_unsold)
        if upload_count == 0:
            upload_skip += 1
            print(
                f"  skip {sku}: supplier has {supplier_unsold} unsold (>= gap {total_gap})",
                file=sys.stderr,
            )
            continue
        idempotency = f"{args.idempotency_prefix}-upload-{sku}"
        prefix = (
            f"{sku}: upload {upload_count} keys "
            f"(supplier had {supplier_unsold}, total gap {total_gap}) [{idempotency}]"
        )
        if args.dry_run:
            print(f"  [dry-run] POST {prefix}", file=sys.stderr)
            continue
        key_lines = [f"BANDAI-{sku}-{uuid4().hex[:16]}" for _ in range(upload_count)]
        body = "\n".join(key_lines) + "\n"
        url = f"https://{args.host}/api/v1/supplier/{args.org_id}/inventory/{sku}/upload"
        resp = post_with_retry(
            session,
            url,
            headers={
                "Authorization": f"Bearer {args.token}",
                "Content-Type": "text/csv",
                "X-Idempotency-Key": idempotency,
            },
            data=body.encode("utf-8"),
        )
        if resp.ok:
            print(f"  ok   {prefix}", file=sys.stderr)
            upload_ok += 1
            if sku not in state["uploads_done"]:
                state["uploads_done"].append(sku)
            save_state(args.state_file, state)
        else:
            print(f"  FAIL {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            upload_fail += 1
        time.sleep(REQUEST_DELAY_S)

    # Group transfers by SKU so we can validate supplier inventory once per SKU.
    transfers_by_sku: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (sku, org), gap in gaps.items():
        if gap > 0:
            transfers_by_sku[sku].append((org, gap))

    print("\n== Top-up transfers ==", file=sys.stderr)
    transfer_ok = transfer_fail = transfer_skip = 0
    for sku in sorted(transfers_by_sku):
        sku_transfers = transfers_by_sku[sku]
        sku_total = sum(g for _, g in sku_transfers)

        if not args.dry_run:
            supplier_unsold = wait_for_supplier_unsold(
                session, args.host, args.org_id, sku, headers, sku_total
            )
            if supplier_unsold < sku_total:
                print(
                    f"  SKIP {sku}: supplier vault has {supplier_unsold} unsold, "
                    f"need {sku_total} for {len(sku_transfers)} transfer(s); "
                    "run reconcile again once the upload Job finishes",
                    file=sys.stderr,
                )
                transfer_skip += len(sku_transfers)
                continue

        for org, gap in sorted(sku_transfers):
            prefix = f"{sku} -> {org}: transfer {gap} keys"
            if args.dry_run:
                print(f"  [dry-run] POST {prefix}", file=sys.stderr)
                continue
            body = {
                "amountOfEntries": gap,
                "oldOwner": args.org_id,
                "newOwner": org,
                "comment": "Reconcile gap-fill",
            }
            url = f"https://{args.host}/api/v1/orgs/{args.org_id}/inventory/{sku}/transfer"
            resp = post_with_retry(session, url, headers=headers, json_body=body)
            if resp.ok:
                print(f"  ok   {prefix}", file=sys.stderr)
                transfer_ok += 1
                if (sku, org) not in state["transfers_done"]:
                    state["transfers_done"].append((sku, org))
                save_state(args.state_file, state)
            else:
                print(f"  FAIL {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
                transfer_fail += 1
            time.sleep(REQUEST_DELAY_S)

    print(
        f"\nDone. uploads ok={upload_ok} fail={upload_fail} skip={upload_skip}; "
        f"transfers ok={transfer_ok} fail={transfer_fail} skip={transfer_skip}",
        file=sys.stderr,
    )
    return 1 if (upload_fail or transfer_fail or transfer_skip) else 0


if __name__ == "__main__":
    sys.exit(main())
