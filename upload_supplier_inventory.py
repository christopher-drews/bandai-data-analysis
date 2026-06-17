"""Upload N synthetic keys to every SKU in the supplier's catalog (no transfer).

Fetches the supplier catalog via /api/v1/lv-team/catalog?supplier=<org_id>
and, for each SKU, POSTs N synthetic keys to
  POST /api/v1/supplier/{org_id}/inventory/{sku_id}/upload

No transfer phase, no sales report — this is upload-only inventory seeding.

Resumable: every successful upload is appended to --state-file (JSON).
Re-running picks up where the last run left off. Transient read timeouts
and 5xx responses are retried with exponential backoff.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import requests

DEFAULT_HOST = "lv.play-asia.com"
DEFAULT_ORG_ID = "org-u1gm1u0j"
DEFAULT_STATE_FILE = Path("data/.upload_supplier_inventory_state.json")
DEFAULT_COUNT = 100
CATALOG_PAGE_SIZE = 1000
REQUEST_DELAY_S = 0.1
TIMEOUT_S = 120
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = (2, 5, 15)


def post_with_retry(
    session: requests.Session,
    url: str,
    *,
    headers: dict,
    data: bytes,
) -> requests.Response:
    last_exc: Exception | None = None
    resp: requests.Response | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = session.post(url, headers=headers, data=data, timeout=TIMEOUT_S)
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


def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    raw = json.loads(path.read_text())
    return set(raw.get("uploads_done", []))


def save_state(path: Path, done: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"uploads_done": sorted(done)}, indent=2))
    tmp.replace(path)


def fetch_catalog_sku_ids(
    session: requests.Session, host: str, org_id: str, headers: dict
) -> list[str]:
    """Return the list of SKU ids for every catalog item attached to this supplier."""
    base = f"https://{host}/api/v1/lv-team/catalog"
    sku_ids: list[str] = []
    seen: set[str] = set()
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
            if sku and sku not in seen:
                seen.add(sku)
                sku_ids.append(sku)
        offset += len(items)
        if len(items) < CATALOG_PAGE_SIZE or offset >= payload.get("total", offset):
            break
    return sku_ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID, help="Supplier organisation id")
    parser.add_argument("--token", required=True, help="Bearer JWT")
    parser.add_argument("--count", default=DEFAULT_COUNT, type=int, help="Keys per SKU (default 100)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--idempotency-prefix", default=f"bandai-inv-{uuid4().hex[:8]}",
                        help="Used to build X-Idempotency-Key per SKU upload")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, type=Path,
                        help="JSON file recording completed uploads; re-runs skip what's already there.")
    args = parser.parse_args()

    if args.count <= 0:
        print(f"--count must be > 0 (got {args.count})", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    session = requests.Session()

    print("Fetching supplier catalog...", file=sys.stderr)
    sku_ids = fetch_catalog_sku_ids(session, args.host, args.org_id, headers)
    print(f"  {len(sku_ids)} SKUs in catalog", file=sys.stderr)
    if not sku_ids:
        print("Nothing to upload.", file=sys.stderr)
        return 0

    done = load_state(args.state_file)
    print(f"State: {len(done)} SKUs already uploaded ({args.state_file})", file=sys.stderr)

    upload_ok = upload_fail = upload_skip = 0
    print(f"\n== Upload phase ({args.count} keys per SKU) ==", file=sys.stderr)
    for sku_id in sorted(sku_ids):
        if sku_id in done:
            upload_skip += 1
            continue
        key_lines = [f"BANDAI-{sku_id}-{uuid4().hex[:16]}" for _ in range(args.count)]
        body = "\n".join(key_lines) + "\n"
        idempotency = f"{args.idempotency_prefix}-upload-{sku_id}"
        prefix = f"{sku_id}: {args.count} keys [{idempotency}]"
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
            done.add(sku_id)
            save_state(args.state_file, done)
        else:
            print(f"  FAIL upload {prefix} -> {resp.status_code} {resp.text}", file=sys.stderr)
            upload_fail += 1
        time.sleep(REQUEST_DELAY_S)

    print(
        f"\nDone. uploads ok={upload_ok} fail={upload_fail} skip={upload_skip} "
        f"({args.count} keys × {upload_ok} SKUs = {args.count * upload_ok} keys uploaded this run)",
        file=sys.stderr,
    )
    return 0 if upload_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
