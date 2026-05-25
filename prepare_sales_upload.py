"""Pre-stage LootVault inventory so the level_2 sales history can be reported.

Reads data/level_2_anonymize_sales_history/product_sales_history.csv and:

  1. Aggregates ``amount`` per ``paxCode`` to get total keys to upload per SKU.
  2. Uploads that many synthetic keys to the supplier vault via
       POST /api/v1/supplier/{org_id}/inventory/{sku_id}/upload
     (CSV body, X-Idempotency-Key header).
  3. Aggregates ``amount`` per (paxCode, Customer) and transfers the
     per-reseller share from the supplier vault to each reseller via
       POST /api/v1/orgs/{supplier_org_id}/inventory/{sku_id}/transfer

paxCode -> sku_id resolution uses the catalog item's ``paPaxCode`` field
fetched from /api/v1/lv-team/catalog?supplier=<org_id>. Customer ->
reseller org_id resolution uses data/customer_org_map.csv. Rows whose
``Customer`` is not in the map (including ``All``) are skipped.

Resumable: every successful upload / transfer is appended to --state-file
(JSON). Re-running picks up where the last run left off. Transient read
timeouts and 5xx responses are retried with exponential backoff.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

import requests

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
DEFAULT_CSV = Path("data/level_2_anonymize_sales_history/product_sales_history.csv")
DEFAULT_CUSTOMER_MAP = Path("data/customer_org_map.csv")
DEFAULT_STATE_FILE = Path("data/.prepare_sales_state.json")
CATALOG_PAGE_SIZE = 1000
REQUEST_DELAY_S = 0.1
TIMEOUT_S = 120
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = (2, 5, 15)  # waits between attempts 1->2, 2->3, 3->4


def post_with_retry(
    session: requests.Session,
    url: str,
    *,
    headers: dict,
    json_body: dict | None = None,
    data: bytes | None = None,
) -> requests.Response:
    """POST with retries on read timeouts and 5xx. Returns the final response."""
    last_exc: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = session.post(
                url,
                headers=headers,
                json=json_body,
                data=data,
                timeout=TIMEOUT_S,
            )
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            resp = None
        else:
            if resp.status_code < 500:
                return resp
            last_exc = RuntimeError(f"{resp.status_code} {resp.text[:200]}")
        if attempt < RETRY_ATTEMPTS - 1:
            wait = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
            print(f"    retry in {wait}s ({last_exc})", file=sys.stderr)
            time.sleep(wait)
    if resp is not None:
        return resp
    raise last_exc  # type: ignore[misc]


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"uploads_done": [], "transfers_done": []}
    raw = json.loads(path.read_text())
    return {
        "uploads_done": list(raw.get("uploads_done", [])),
        "transfers_done": [tuple(t) for t in raw.get("transfers_done", [])],
    }


def save_state(path: Path, state: dict) -> None:
    serializable = {
        "uploads_done": sorted(set(state["uploads_done"])),
        "transfers_done": sorted({tuple(t) for t in state["transfers_done"]}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serializable, indent=2))
    tmp.replace(path)


def fetch_catalog_by_paxcode(
    session: requests.Session, host: str, org_id: str, headers: dict
) -> dict[str, str]:
    """Return {paPaxCode: skuId} for every catalog item attached to this supplier."""
    base = f"https://{host}/api/v1/lv-team/catalog"
    by_pax: dict[str, str] = {}
    duplicates: dict[str, list[str]] = defaultdict(list)
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
            sku = item.get("id")
            if not pax or not sku:
                continue
            if pax in by_pax and by_pax[pax] != sku:
                duplicates[pax].append(sku)
            by_pax[pax] = sku
        offset += len(items)
        if len(items) < CATALOG_PAGE_SIZE or offset >= payload.get("total", offset):
            break

    for pax, skus in duplicates.items():
        print(f"  warn: paxCode {pax!r} maps to multiple SKUs: {[by_pax[pax], *skus]}", file=sys.stderr)
    return by_pax


def load_customer_org_map(path: Path) -> dict[str, str]:
    """Return {customer_name: org_id} keyed on the Customer column from the sales CSV."""
    mapping: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            org = (row.get("org_id") or "").strip()
            if not name or not org:
                continue
            mapping[name] = org
    return mapping


def amount_as_int(raw: str) -> int:
    try:
        return max(0, round(float(raw)))
    except (TypeError, ValueError):
        return 0


def aggregate(rows: list[dict], pax_to_sku: dict[str, str], cust_to_org: dict[str, str]) -> tuple[
    dict[str, int],
    dict[tuple[str, str], int],
    int,
    int,
]:
    """Return (per-sku total, per-(sku, reseller) total, skipped_pax, skipped_cust)."""
    per_sku: dict[str, int] = defaultdict(int)
    per_sku_reseller: dict[tuple[str, str], int] = defaultdict(int)
    skipped_pax = skipped_cust = 0
    for row in rows:
        amount = amount_as_int(row.get("amount", ""))
        if amount <= 0:
            continue
        pax = (row.get("paxCode") or "").strip()
        customer = (row.get("Customer") or "").strip()
        sku = pax_to_sku.get(pax)
        if not sku:
            skipped_pax += 1
            continue
        org = cust_to_org.get(customer)
        if not org:
            skipped_cust += 1
            continue
        per_sku[sku] += amount
        per_sku_reseller[(sku, org)] += amount
    return per_sku, per_sku_reseller, skipped_pax, skipped_cust


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV, type=Path)
    parser.add_argument("--customer-org-map", default=DEFAULT_CUSTOMER_MAP, type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID, help="Supplier organisation id")
    parser.add_argument("--token", required=True, help="Bearer JWT")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--idempotency-prefix", default=f"bandai-{uuid4().hex[:8]}",
                        help="Used to build X-Idempotency-Key per SKU upload")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, type=Path,
                        help="JSON file recording completed uploads/transfers; "
                             "re-runs skip what's already there.")
    parser.add_argument("--skip-uploads", action="store_true",
                        help="Treat the upload phase as already complete (uses state for the transfer phase only).")
    args = parser.parse_args()

    with args.csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    cust_to_org = load_customer_org_map(args.customer_org_map)
    if not cust_to_org:
        print(
            f"error: {args.customer_org_map} has no usable rows "
            "(populate it with name,org_id rows before running)",
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

    per_sku, per_sku_reseller, skipped_pax, skipped_cust = aggregate(rows, pax_to_sku, cust_to_org)
    total_keys = sum(per_sku.values())
    print(
        f"Aggregated: {len(per_sku)} SKUs, {total_keys} total keys, "
        f"{len(per_sku_reseller)} (sku, reseller) transfers. "
        f"Skipped {skipped_pax} unmatched-pax rows, {skipped_cust} unmatched-customer rows.",
        file=sys.stderr,
    )
    if total_keys == 0:
        print("Nothing to upload.", file=sys.stderr)
        return 0

    state = load_state(args.state_file)
    if args.skip_uploads:
        state["uploads_done"] = sorted(set(state["uploads_done"]) | set(per_sku))
    done_uploads = set(state["uploads_done"])
    done_transfers = {tuple(t) for t in state["transfers_done"]}
    print(
        f"State: {len(done_uploads)} uploads already done, "
        f"{len(done_transfers)} transfers already done "
        f"({args.state_file})",
        file=sys.stderr,
    )

    upload_ok = upload_fail = upload_skip = 0
    print("\n== Upload phase ==", file=sys.stderr)
    for sku_id, count in sorted(per_sku.items()):
        if sku_id in done_uploads:
            upload_skip += 1
            continue
        key_lines = [f"BANDAI-{sku_id}-{uuid4().hex[:16]}" for _ in range(count)]
        body = "\n".join(key_lines) + "\n"
        idempotency = f"{args.idempotency_prefix}-upload-{sku_id}"
        prefix = f"{sku_id}: {count} keys [{idempotency}]"
        if args.dry_run:
            print(f"  [dry-run] POST upload {prefix}", file=sys.stderr)
            continue
        url = f"https://{args.host}/api/v1/supplier/{args.org_id}/inventory/{sku_id}/upload"
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
            print(f"  ok   upload {prefix}", file=sys.stderr)
            upload_ok += 1
            state["uploads_done"].append(sku_id)
            save_state(args.state_file, state)
        else:
            print(f"  FAIL upload {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            upload_fail += 1
        time.sleep(REQUEST_DELAY_S)

    transfer_ok = transfer_fail = transfer_skip = 0
    print("\n== Transfer phase ==", file=sys.stderr)
    for (sku_id, reseller_org), count in sorted(per_sku_reseller.items()):
        if (sku_id, reseller_org) in done_transfers:
            transfer_skip += 1
            continue
        body = {
            "amountOfEntries": count,
            "oldOwner": args.org_id,
            "newOwner": reseller_org,
            "comment": "Synthetic bandai backfill",
        }
        prefix = f"{sku_id} -> {reseller_org}: {count} keys"
        if args.dry_run:
            print(f"  [dry-run] POST transfer {prefix}", file=sys.stderr)
            continue
        url = f"https://{args.host}/api/v1/orgs/{args.org_id}/inventory/{sku_id}/transfer"
        resp = post_with_retry(session, url, headers=headers, json_body=body)
        if resp.ok:
            print(f"  ok   transfer {prefix}", file=sys.stderr)
            transfer_ok += 1
            state["transfers_done"].append((sku_id, reseller_org))
            save_state(args.state_file, state)
        else:
            print(f"  FAIL transfer {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            transfer_fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(
        f"\nDone. uploads ok={upload_ok} fail={upload_fail} skip={upload_skip}; "
        f"transfers ok={transfer_ok} fail={transfer_fail} skip={transfer_skip}",
        file=sys.stderr,
    )
    return 1 if (upload_fail or transfer_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
