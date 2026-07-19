cd /Users/christopherdrews/dev/lootvault
B=/Users/christopherdrews/dev/bandai-data-analysis
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

# Heybox
./target/release/lootvault_cli --base-url https://bandai.knoxkee.io --email christopher.drews@play-asia.com --password test1234 \
  testdata add-sales --reseller-org-id org-pq6gycjv --file $B/data/build_add_sales_csv/heybox.csv \
  --database-url "$DATABASE_URL"
# # Sonkwo
# ./target/release/lootvault_cli --base-url https://bandai.knoxkee.io --email <e> --password <p> \
#   testdata add-sales --reseller-org-id org-tapmbbts --file $B/data/build_add_sales_csv/sonkwo.csv \
#   --database-url "$DATABASE_URL"
# # Backdate keys + transfers (add-sales backdates the SALES, not key created_at/transfers)
# psql "$DATABASE_URL" -v supplier_org='org-si2kkmp5' -v created_date='2024-07-01' \
#   -f $B/sql/backdate_bandai_inventory.sql