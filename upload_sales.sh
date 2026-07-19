cd /Users/christopherdrews/dev/lootvault
B=/Users/christopherdrews/dev/bandai-data-analysis
KV="lootvault-bandai-kv";
LOCAL_PORT=5448;

# Host of the local port-forward.
LOCAL_HOST="${LOCAL_HOST:-localhost}"

# Reseller to upload (folder under data/build_add_sales_csv/) and how many
# uploads to run at once. Override on the CLI: ./upload_sales.sh sonkwo 8
RESELLER="${1:-heybox}"
PARALLEL="${2:-${PARALLEL:-4}}"

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

# Reseller name -> org id (see data/customer_org_map.csv).
case "$RESELLER" in
  heybox) ORG_ID="org-pq6gycjv" ;;
  sonkwo) ORG_ID="org-tapmbbts" ;;
  *) echo "Unknown reseller '$RESELLER' (expected heybox|sonkwo)" >&2; exit 1 ;;
esac

CSV_DIR="$B/data/build_add_sales_csv/$RESELLER"
if [ ! -d "$CSV_DIR" ]; then
  echo "CSV dir not found: $CSV_DIR" >&2
  exit 1
fi

LOG_DIR="$B/logs/upload_sales/$RESELLER"
mkdir -p "$LOG_DIR"

# Work queue: one line per per-SKU CSV. Workers pop lines atomically.
QUEUE_FILE="$LOG_DIR/.queue"
LOCK_DIR="$LOG_DIR/.lock"
rmdir "$LOCK_DIR" 2>/dev/null
ls "$CSV_DIR"/*.csv > "$QUEUE_FILE" 2>/dev/null
TOTAL=$(grep -c . "$QUEUE_FILE")
if [ "$TOTAL" -eq 0 ]; then
  echo "No CSVs in $CSV_DIR" >&2
  exit 1
fi

echo "Uploading $TOTAL CSV(s) for $RESELLER ($ORG_ID) with $PARALLEL worker(s)."
echo "Logs: $LOG_DIR/worker_*.log"

# Atomically pop the first queued file. mkdir is atomic across processes, so it
# serves as a lock (macOS has no flock). Prints the path and returns 0, or
# returns 1 when the queue is empty.
pop_task() {
  local first rc=1
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do sleep 0.05; done
  first=$(head -n 1 "$QUEUE_FILE")
  if [ -n "$first" ]; then
    tail -n +2 "$QUEUE_FILE" > "$QUEUE_FILE.tmp" && mv "$QUEUE_FILE.tmp" "$QUEUE_FILE"
    printf '%s\n' "$first"
    rc=0
  fi
  rmdir "$LOCK_DIR"
  return $rc
}

# A single worker: keep pulling the next CSV until the queue drains, appending
# all output to its own log file.
worker() {
  local wid="$1"
  local log="$LOG_DIR/worker_${wid}.log"
  : > "$log"
  local file
  while file=$(pop_task); do
    echo "==== $(date '+%Y-%m-%d %H:%M:%S') uploading $(basename "$file") ====" >> "$log"
    ./target/release/lootvault_cli \
      --base-url https://bandai.knoxkee.io \
      --email christopher.drews@play-asia.com --password test1234 \
      testdata add-sales --reseller-org-id "$ORG_ID" --file "$file" \
      --database-url "$DATABASE_URL" --backdate >> "$log" 2>&1 
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') done $(basename "$file") (exit $?) ----" >> "$log"
  done
}

for i in $(seq 1 "$PARALLEL"); do
  worker "$i" &
done
wait

rm -f "$QUEUE_FILE" "$QUEUE_FILE.tmp"
echo "All uploads finished for $RESELLER."

# # Backdate keys + transfers (add-sales backdates the SALES, not key created_at/transfers)
# psql "$DATABASE_URL" -v supplier_org='org-si2kkmp5' -v created_date='2024-07-01' \
#   -f $B/sql/backdate_bandai_inventory.sql
