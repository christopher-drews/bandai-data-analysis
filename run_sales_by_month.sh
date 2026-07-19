#!/usr/bin/env bash
# Per-month sales lifecycle for the bandai env.
#
# For each month (ascending), runs the FULL circle so the inventory timeline is
# month-by-month, not bulk-upfront:
#   1. prepare_sales_upload --month M   stage that month's keys + transfer shares
#   2. upload_sales_history  --month M   report that month's sales
#   3. backdate SQL (created_date = M-01) backdate that month's keys/transfers to M
#
# Because the backdate's `created_at > :created_date` guards skip already-backdated
# (earlier) months, running months in ascending order backdates each month's new
# rows to that month and never re-touches prior months. Per-month --state-file and
# --idempotency-prefix keep prepare's per-sku state from colliding across months.
#
# Usage:
#   BANDAI_EMAIL=<e> BANDAI_PASSWORD=<p> DATABASE_URL=<psql url via db-proxy> \
#     ./run_sales_by_month.sh [--dry-run]
# Auth (one of; credentials are recommended for a long run so the token auto-refreshes):
#   BANDAI_EMAIL + BANDAI_PASSWORD  -> scripts re-authenticate on 401 (no mid-run expiry)
#   BANDAI_TOKEN                    -> static JWT (may expire during the run)
# Env:
#   DATABASE_URL  (required unless dry-run) psql URL through the db-proxy tunnel (backdate)
#   HOST          default bandai.knoxkee.io
#   ORG_ID        default org-si2kkmp5      (bandai supplier)
#   CSV           default data/level_1_extract_sales_history/product_sales_history.csv
set -euo pipefail

DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="--dry-run"
HOST="${HOST:-bandai.knoxkee.io}"
ORG_ID="${ORG_ID:-org-si2kkmp5}"
CSV="${CSV:-data/level_1_extract_sales_history/product_sales_history.csv}"
PY="${PY:-.venv/bin/python}"
BANDAI_EMAIL="${BANDAI_EMAIL:-christopher.drews@play-asia.com}"
BANDAI_PASSWORD="${BANDAI_PASSWORD:-test1234}"
KV="lootvault-bandai-kv"; 
LOCAL_PORT=5448;

# Host of the local port-forward.
LOCAL_HOST="${LOCAL_HOST:-localhost}"

if ! command -v az >/dev/null 2>&1; then
  echo "az CLI not found. Install it and run 'az login'." >&2
  exit 1
fi
if ! az account show >/dev/null 2>&1; then
  echo "Not logged in to Azure. Run 'az login' first." >&2
  exit 1
fi

# postgres://USER:PASSWORD@HOST:PORT/DATABASE[?params]
DSN=$(az keyvault secret show --vault-name "$KV" --name database-url --query value -o tsv)
if [ -z "$DSN" ]; then
  echo "Empty 'database-url' secret from vault $KV" >&2
  exit 1
fi

# Swap the authority host:port (the segment after the last '@', up to the path or
# query) for the local forward. Credentials before the '@' are left untouched.
DSN=$(printf '%s' "$DSN" | sed -E "s#@[^@/]+(/|\?|\$)#@${LOCAL_HOST}:${LOCAL_PORT}\1#")

# Force sslmode=require (NOT verify-full): TLS is end-to-end to the real server,
# whose cert CN is the private FQDN, not localhost, so hostname verification under
# verify-full would fail. Normalise whatever sslmode the vault DSN carried.
if printf '%s' "$DSN" | grep -q 'sslmode='; then
  DSN=$(printf '%s' "$DSN" | sed -E 's#sslmode=[^&]*#sslmode=require#')
elif printf '%s' "$DSN" | grep -q '?'; then
  DSN="${DSN}&sslmode=require"
else
  DSN="${DSN}?sslmode=require"
fi
export DATABASE_URL="$DSN"



if [ -n "${BANDAI_EMAIL:-}" ] && [ -n "${BANDAI_PASSWORD:-}" ]; then
  AUTH=(--email "$BANDAI_EMAIL" --password "$BANDAI_PASSWORD")
elif [ -n "${BANDAI_TOKEN:-}" ]; then
  AUTH=(--token "$BANDAI_TOKEN")
else
  echo "Set BANDAI_EMAIL + BANDAI_PASSWORD (recommended) or BANDAI_TOKEN." >&2
  exit 1
fi

# Months present for the in-scope resellers, ascending.
MONTHS=$("$PY" - "$CSV" <<'EOF'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
ms = sorted({r["start_month"] for r in rows
             if r.get("Customer") in ("Heybox", "Sonkwo") and r.get("start_month")})
print(" ".join(ms))
EOF
)
echo "Months to process: $MONTHS"

for M in $MONTHS; do
  echo "===================== $M ====================="
  "$PY" prepare_sales_upload.py --host "$HOST" --org-id "$ORG_ID" "${AUTH[@]}" \
    --csv "$CSV" --month "$M" \
    --state-file "data/.prepare_${M}.json" --idempotency-prefix "bandai-${M}" $DRY
  "$PY" upload_sales_history.py --host "$HOST" --org-id "$ORG_ID" "${AUTH[@]}" \
    --csv "$CSV" --month "$M" --state-file "data/.upload_${M}.json" $DRY
  if [ -z "$DRY" ]; then
    : "${DATABASE_URL:?set DATABASE_URL (db-proxy tunnel) for the backdate step}"
    psql "$DATABASE_URL" -v supplier_org="$ORG_ID" -v created_date="${M}-01" \
      -f sql/backdate_bandai_inventory.sql
  fi
done

echo "Done. Optional final check: reconcile_sales_inventory.py --host $HOST --org-id $ORG_ID --token <JWT> --csv $CSV"
