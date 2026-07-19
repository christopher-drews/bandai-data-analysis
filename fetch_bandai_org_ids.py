"""Fetch a target env's organisation ids and write the reseller org map.

The live-API sales scripts (prepare_sales_upload / upload_sales_history /
reconcile_sales_inventory) resolve resellers via data/customer_org_map.csv and the
supplier via --org-id. For a freshly-seeded environment (e.g. bandai.knoxkee.io)
those ids are server-generated, so the committed map (lv.play-asia ids) is wrong.

This helper logs in (or takes a --token), looks up each org by name via the
LootVault identity API, then:
  - writes data/customer_org_map.csv (`name,org_id`) for the resellers (Heybox,
    Sonkwo — Alibaba is out of scope), and
  - prints the supplier org id to stdout for use as the scripts' --org-id.

Endpoints (same ones the lootvault scenario CLI uses):
  POST /api/identity/v1/user/login            {email,password} -> {accessToken}
  GET  /api/identity/v1/organisation?search=  -> {items:[{organisationId,name,...}]}
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import requests

DEFAULT_HOST = "bandai.knoxkee.io"
DEFAULT_OUTPUT = Path("data/customer_org_map.csv")
DEFAULT_SUPPLIER = "Bandai Namco"
DEFAULT_RESELLERS = "Heybox,Sonkwo"
TIMEOUT_S = 30


def base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host


def login(session: requests.Session, base: str, email: str, password: str) -> str:
    resp = session.post(
        f"{base}/api/identity/v1/user/login",
        json={"email": email, "password": password},
        timeout=TIMEOUT_S,
    )
    if not resp.ok:
        raise SystemExit(f"Login failed ({resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("accessToken")
    if not token:
        raise SystemExit("Login response had no accessToken")
    return token


def find_org(session: requests.Session, base: str, headers: dict, name: str) -> str:
    """Return the organisationId whose name matches ``name`` exactly (case-insensitive)."""
    resp = session.get(
        f"{base}/api/identity/v1/organisation",
        params={"search": name, "limit": 1000},
        headers=headers,
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    matches = [it for it in items if (it.get("name") or "").strip().lower() == name.strip().lower()]
    if not matches:
        seen = ", ".join(sorted((it.get("name") or "") for it in items)) or "(none)"
        raise SystemExit(f"No org named {name!r} found. search returned: {seen}")
    if len(matches) > 1:
        ids = [m.get("organisationId") for m in matches]
        raise SystemExit(f"Multiple orgs named {name!r}: {ids}")
    return matches[0]["organisationId"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--token", help="Bearer JWT; if omitted, --email/--password are used to log in")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument("--supplier-name", default=DEFAULT_SUPPLIER)
    parser.add_argument("--resellers", default=DEFAULT_RESELLERS,
                        help="Comma-separated reseller org names to write to the map")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    base = base_url(args.host)
    session = requests.Session()

    if args.token:
        token = args.token
    elif args.email and args.password:
        token = login(session, base, args.email, args.password)
        print("Logged in.", file=sys.stderr)
    else:
        raise SystemExit("Provide --token, or both --email and --password.")
    headers = {"Authorization": f"Bearer {token}"}

    reseller_names = [n.strip() for n in args.resellers.split(",") if n.strip()]
    reseller_rows = [(name, find_org(session, base, headers, name)) for name in reseller_names]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["name", "org_id"])
        writer.writerows(reseller_rows)
    print(f"Wrote {args.output}:", file=sys.stderr)
    for name, org_id in reseller_rows:
        print(f"  {name} -> {org_id}", file=sys.stderr)

    supplier_id = find_org(session, base, headers, args.supplier_name)
    print(f"\nSupplier {args.supplier_name!r} org id (use as --org-id):", file=sys.stderr)
    print(supplier_id)  # stdout, so it can be captured
    return 0


if __name__ == "__main__":
    sys.exit(main())
