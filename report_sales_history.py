"""Merged LootVault sales pipeline: upload + transfer + report + confirm.

For every (month, sku, reseller) unit derived from
data/level_2_anonymize_sales_history/<month>_<customer>.csv this script:

  1. Uploads N synthetic keys to the supplier vault
       POST /api/v1/supplier/{supplier_org}/inventory/{sku}/upload
  2. Waits until the supplier vault's unsold count rises by at least N.
  3. Transfers N keys from the supplier vault to the reseller
       POST /api/v1/orgs/{supplier_org}/inventory/{sku}/transfer
  4. Waits until the reseller's unsold count rises by at least N.
  5. Fetches N unsold reseller keys and submits a JSON report
       POST /api/v1/reseller/{reseller_org}/reports/json
  6. Confirms (applies) the report
       POST /api/v1/reseller/{reseller_org}/reports/{report_id}/confirm

Iteration order: months **newest first**; within each month, SKUs are
processed **ascending by per-month total amount** (smallest first); within
each (month, sku), resellers in alphabetical order.

Resumable: progress is persisted per (month, sku, reseller) to
data/.report_sales_state.json. A unit that crashed mid-flight (e.g. report
submitted but confirm never returned) is picked up at the right step on
re-run.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    post_with_retry,
)
from reconcile_sales_inventory import count_unsold_keys
from upload_sales_history import (
    fetch_reseller_keys,
    month_window,
    random_timestamps,
)

DEFAULT_INPUT_DIR = Path("data/level_2_anonymize_sales_history")
DEFAULT_STATE_FILE = Path("data/.report_sales_state.json")

# Async-ingest polling for supplier upload + reseller transfer.
POLL_ATTEMPTS = 24
POLL_DELAY_S = 5

FILENAME_RE = re.compile(r"^(?P<month>\d{4}-\d{2})_(?P<customer>.+)\.csv$")


def unit_key(month: str, sku: str, reseller_org: str) -> str:
    return f"{month}|{sku}|{reseller_org}"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"units": {}}
    text = path.read_text().strip()
    if not text:
        return {"units": {}}
    raw = json.loads(text)
    return {"units": dict(raw.get("units", {}))}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def discover_inputs(input_dir: Path) -> list[tuple[str, str, Path]]:
    """Return [(month, customer, csv_path)] for every YYYY-MM_<customer>.csv."""
    found = []
    for path in sorted(input_dir.glob("*.csv")):
        match = FILENAME_RE.match(path.name)
        if not match:
            print(f"  skip: {path.name} does not match YYYY-MM_<customer>.csv", file=sys.stderr)
            continue
        found.append((match.group("month"), match.group("customer"), path))
    return found


def wait_for_count_at_least(
    fetch: callable,
    target: int,
    label: str,
) -> int:
    """Poll ``fetch`` until it returns >= ``target`` or attempts exhaust."""
    last = 0
    for attempt in range(POLL_ATTEMPTS):
        last = fetch()
        if last >= target:
            return last
        if attempt < POLL_ATTEMPTS - 1:
            print(
                f"    {label}: have {last}/{target} — waiting {POLL_DELAY_S}s",
                file=sys.stderr,
            )
            time.sleep(POLL_DELAY_S)
    return last


def extract_report_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "reportId", "report_id", "requestId", "request_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    request = payload.get("request")
    if isinstance(request, dict):
        return extract_report_id(request)
    return None


def process_unit(
    *,
    session: requests.Session,
    args: argparse.Namespace,
    headers_json: dict,
    bearer_only: dict,
    state: dict,
    rng: random.Random,
    month: str,
    sku: str,
    reseller_org: str,
    specs: list[dict],
) -> str:
    """Run upload->transfer->report->confirm for one (month, sku, reseller).

    Returns one of: "done", "skip", "fail".
    """
    key = unit_key(month, sku, reseller_org)
    unit_state = dict(state["units"].get(key, {}))
    if unit_state.get("confirmed"):
        return "skip"

    count = sum(s["count"] for s in specs)
    if count <= 0:
        return "skip"

    prefix = f"{month} {sku} {reseller_org} ({count} keys)"
    idempotency = f"{args.idempotency_prefix}-upload-{month}-{sku}-{reseller_org}"

    if args.dry_run:
        print(f"  [dry-run] {prefix}: upload + transfer + report + confirm", file=sys.stderr)
        return "done"

    # 1. Upload.
    if not unit_state.get("uploaded"):
        baseline = count_unsold_keys(
            session, args.host, args.org_id, sku, headers_json, placement="vault"
        )
        unit_state["supplier_baseline"] = baseline
        key_lines = [f"BANDAI-{sku}-{uuid4().hex}" for _ in range(count)]
        body = ("\n".join(key_lines) + "\n").encode("utf-8")
        url = f"https://{args.host}/api/v1/supplier/{args.org_id}/inventory/{sku}/upload"
        resp = post_with_retry(
            session,
            url,
            headers={
                **bearer_only,
                "Content-Type": "text/csv",
                "X-Idempotency-Key": idempotency,
            },
            data=body,
        )
        if not resp.ok:
            print(f"  FAIL upload {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            return "fail"
        unit_state["uploaded"] = True
        unit_state["upload_idempotency"] = idempotency
        state["units"][key] = unit_state
        save_state(args.state_file, state)
        print(f"  ok   upload   {prefix}", file=sys.stderr)
        time.sleep(REQUEST_DELAY_S)

    baseline_supplier = unit_state.get("supplier_baseline", 0)
    final = wait_for_count_at_least(
        lambda: count_unsold_keys(
            session, args.host, args.org_id, sku, headers_json, placement="vault"
        ),
        baseline_supplier + count,
        label=f"supplier {sku}",
    )
    if final < baseline_supplier + count:
        print(
            f"  FAIL supplier {prefix}: baseline {baseline_supplier}, "
            f"want +{count}, have {final}",
            file=sys.stderr,
        )
        return "fail"

    # 2. Transfer.
    if not unit_state.get("transferred"):
        baseline_reseller = count_unsold_keys(
            session, args.host, reseller_org, sku, headers_json, placement="reseller"
        )
        unit_state["reseller_baseline"] = baseline_reseller
        url = f"https://{args.host}/api/v1/orgs/{args.org_id}/inventory/{sku}/transfer"
        body = {
            "amountOfEntries": count,
            "oldOwner": args.org_id,
            "newOwner": reseller_org,
            "comment": f"Bandai pipeline {month}",
        }
        resp = post_with_retry(session, url, headers=headers_json, json_body=body)
        if not resp.ok:
            print(
                f"  FAIL transfer {prefix} -> {resp.status_code} {resp.text}",
                file=sys.stderr,
            )
            return "fail"
        unit_state["transferred"] = True
        state["units"][key] = unit_state
        save_state(args.state_file, state)
        print(f"  ok   transfer {prefix}", file=sys.stderr)
        time.sleep(REQUEST_DELAY_S)

    baseline_reseller = unit_state.get("reseller_baseline", 0)
    final_reseller = wait_for_count_at_least(
        lambda: count_unsold_keys(
            session, args.host, reseller_org, sku, headers_json, placement="reseller"
        ),
        baseline_reseller + count,
        label=f"reseller {sku} @ {reseller_org}",
    )
    if final_reseller < baseline_reseller + count:
        print(
            f"  FAIL reseller {prefix}: baseline {baseline_reseller}, "
            f"want +{count}, have {final_reseller}",
            file=sys.stderr,
        )
        return "fail"

    # 3. Submit report.
    if not unit_state.get("report_id"):
        ids = fetch_reseller_keys(
            session, args.host, reseller_org, sku, headers_json, count
        )
        if len(ids) < count:
            print(
                f"  FAIL fetch-keys {prefix}: only {len(ids)}/{count} reseller keys",
                file=sys.stderr,
            )
            return "fail"
        chosen = ids[:count]
        cursor = 0
        entries: list[dict] = []
        for spec in specs:
            window_start, window_end = month_window(spec["start_month"], spec["end_month"])
            timestamps = random_timestamps(rng, window_start, window_end, spec["count"])
            for keyid, ts in zip(chosen[cursor:cursor + spec["count"]], timestamps, strict=True):
                entries.append({
                    "skuId": sku,
                    "keyId": keyid,
                    "date": ts,
                    "price": spec["price"],
                    "currency": spec["currency"],
                })
            cursor += spec["count"]

        url = f"https://{args.host}/api/v1/reseller/{reseller_org}/reports/json"
        resp = post_with_retry(session, url, headers=headers_json, json_body={"entries": entries})
        if not resp.ok:
            print(
                f"  FAIL report {prefix} -> {resp.status_code} {resp.text}",
                file=sys.stderr,
            )
            return "fail"
        report_id = extract_report_id(resp.json() if resp.text else None)
        if not report_id:
            print(
                f"  FAIL report {prefix}: no report_id in response {resp.text[:200]}",
                file=sys.stderr,
            )
            return "fail"
        unit_state["report_id"] = report_id
        unit_state["report_submitted_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["units"][key] = unit_state
        save_state(args.state_file, state)
        print(f"  ok   report   {prefix} -> {report_id}", file=sys.stderr)
        time.sleep(REQUEST_DELAY_S)

    # 4. Confirm.
    report_id = unit_state["report_id"]
    url = f"https://{args.host}/api/v1/reseller/{reseller_org}/reports/{report_id}/confirm"
    resp = post_with_retry(session, url, headers=headers_json, json_body={})
    if resp.status_code == 409:
        print(f"  ok   confirm  {prefix} -> already applied ({report_id})", file=sys.stderr)
    elif not resp.ok:
        print(
            f"  FAIL confirm {prefix} ({report_id}) -> {resp.status_code} {resp.text}",
            file=sys.stderr,
        )
        return "fail"
    else:
        print(f"  ok   confirm  {prefix} -> {report_id}", file=sys.stderr)

    unit_state["confirmed"] = True
    state["units"][key] = unit_state
    save_state(args.state_file, state)
    time.sleep(REQUEST_DELAY_S)
    return "done"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, type=Path,
                        help="Directory of per-(month, customer) CSVs from level_2_anonymize_sales_history.")
    parser.add_argument("--customer-org-map", default=DEFAULT_CUSTOMER_MAP, type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID, help="Supplier organisation id")
    parser.add_argument("--token", required=True, help="Bearer JWT")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for timestamp generation")
    parser.add_argument("--month", default=None, help="Optional YYYY-MM filter")
    parser.add_argument("--reseller", default=None,
                        help="Optional reseller name filter (matches Customer column, e.g. 'Heybox')")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, type=Path,
                        help="JSON file tracking per-(month, sku, reseller) progress. Re-runs resume from it.")
    parser.add_argument("--idempotency-prefix", default=f"bandai-{uuid4().hex[:8]}",
                        help="Used to build X-Idempotency-Key per (month, sku, reseller) upload")
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        print(f"error: input dir not found: {args.input_dir}", file=sys.stderr)
        return 1

    cust_to_org = load_customer_org_map(args.customer_org_map)
    if not cust_to_org:
        print(
            f"error: {args.customer_org_map} has no usable rows "
            "(populate it with name,org_id rows before running)",
            file=sys.stderr,
        )
        return 1
    print(f"Loaded {len(cust_to_org)} customer->org mappings", file=sys.stderr)

    headers_json = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    bearer_only = {"Authorization": f"Bearer {args.token}"}
    session = requests.Session()

    print("Fetching supplier catalog...", file=sys.stderr)
    pax_to_sku = fetch_catalog_by_paxcode(session, args.host, args.org_id, headers_json)
    print(f"  {len(pax_to_sku)} SKUs indexed by paxCode", file=sys.stderr)

    rng = random.Random(args.seed)

    inputs = discover_inputs(args.input_dir)
    if not inputs:
        print(f"No CSVs in {args.input_dir}", file=sys.stderr)
        return 1

    # Group rows: (month, sku, reseller_org) -> [spec, ...]
    # Track per-(month, sku) total for SKU-ordering.
    units: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    per_month_sku_total: dict[tuple[str, str], int] = defaultdict(int)
    skipped_pax = skipped_cust = skipped_amount = skipped_filter = 0

    for month, customer, path in inputs:
        if args.month and month != args.month:
            skipped_filter += 1
            continue
        if args.reseller and customer != args.reseller:
            skipped_filter += 1
            continue
        reseller_org = cust_to_org.get(customer)
        if not reseller_org:
            skipped_cust += 1
            print(f"  skip: {path.name} (customer {customer!r} not in map)", file=sys.stderr)
            continue
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                amount = amount_as_int(row.get("amount", ""))
                if amount <= 0:
                    skipped_amount += 1
                    continue
                pax = (row.get("paxCode") or "").strip()
                sku_id = pax_to_sku.get(pax)
                if not sku_id:
                    skipped_pax += 1
                    continue
                try:
                    price = float(row.get("selling_price") or 0.0)
                except ValueError:
                    price = 0.0
                currency = (row.get("currency") or "").strip() or "CNY"
                end_month = (row.get("end_month") or month).strip()
                units[(month, sku_id, reseller_org)].append({
                    "count": amount,
                    "price": round(price, 2),
                    "currency": currency,
                    "start_month": month,
                    "end_month": end_month,
                })
                per_month_sku_total[(month, sku_id)] += amount

    print(
        f"Grouped {len(units)} (month, sku, reseller) units across "
        f"{len({m for (m, _, _) in units})} months. "
        f"Skipped: pax={skipped_pax} customer={skipped_cust} "
        f"amount={skipped_amount} filter={skipped_filter}",
        file=sys.stderr,
    )

    state = load_state(args.state_file)
    confirmed_already = sum(1 for v in state["units"].values() if v.get("confirmed"))
    print(
        f"State: {confirmed_already}/{len(state['units'])} units confirmed in {args.state_file}",
        file=sys.stderr,
    )

    months_desc = sorted({m for (m, _, _) in units}, reverse=True)
    counts = {"done": 0, "skip": 0, "fail": 0}

    for month in months_desc:
        month_skus = sorted(
            {sku for (m, sku, _) in units if m == month},
            key=lambda s: (per_month_sku_total[(month, s)], s),
        )
        print(
            f"\n== {month} ({len(month_skus)} SKUs, ascending by amount) ==",
            file=sys.stderr,
        )
        for sku in month_skus:
            resellers = sorted({r for (m, s, r) in units if m == month and s == sku})
            for reseller_org in resellers:
                outcome = process_unit(
                    session=session,
                    args=args,
                    headers_json=headers_json,
                    bearer_only=bearer_only,
                    state=state,
                    rng=rng,
                    month=month,
                    sku=sku,
                    reseller_org=reseller_org,
                    specs=units[(month, sku, reseller_org)],
                )
                counts[outcome] += 1

    print(
        f"\nDone. units done={counts['done']} skip={counts['skip']} fail={counts['fail']}",
        file=sys.stderr,
    )
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
