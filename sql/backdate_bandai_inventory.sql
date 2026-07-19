-- Backdate synthetic Bandai inventory so keys and transfers predate their sales.
--
-- The report API backdates the SALE (vault.product_keys.selling_date), but key
-- creation (created_at) and transfer audit rows are hardwired to now() with no API
-- affordance. Without this fix a key "created today" carries a 2025 sale -> a
-- sold-before-created row. Run this as the FINAL realism pass, after
-- prepare_sales_upload.py + upload_sales_history.py, over the db-proxy tunnel:
--
--   psql "$DATABASE_URL" \
--     -v supplier_org='org-XXXXXXXX' \
--     -v created_date='2024-07-01' \
--     -f sql/backdate_bandai_inventory.sql
--
-- supplier_org = the Bandai supplier org id (printed by fetch_bandai_org_ids.py).
-- created_date = a pre-sales launch date (default 2024-07-01; earliest sale is 2024-08).
-- selling_date (the real, already-backdated sale time) is left untouched.
-- Uses the env's DML app role (has UPDATE); safe to re-run (idempotent — the
-- `> created_date` guards make a second run a no-op).

\set ON_ERROR_STOP on

-- Require supplier_org; default created_date.
\if :{?supplier_org}
\else
  \warn 'ERROR: pass -v supplier_org=org-XXXX'
  \quit
\endif
\if :{?created_date}
\else
  \set created_date '2024-07-01'
\endif

-- Preview: rows that will change.
\echo 'Rows to backdate (supplier' :'supplier_org' 'before' :'created_date' '):'
SELECT 'product_keys' AS table, count(*) AS rows
  FROM vault.product_keys
  WHERE sku_supplier = :'supplier_org' AND created_at > :'created_date'::timestamptz
UNION ALL
SELECT 'transactions', count(*)
  FROM vault.transactions
  WHERE :'supplier_org' = ANY (affected_orgs) AND ts > :'created_date'::timestamptz
UNION ALL
SELECT 'jobs', count(*)
  FROM vault.jobs
  WHERE organisation_id = :'supplier_org' AND start_ts > :'created_date'::timestamptz;

BEGIN;

-- 1) Keys: created_at -> launch date; updated_at -> sale time when sold, else launch;
--    any lingering in-flight transfer marker -> launch too.
UPDATE vault.product_keys
SET created_at = :'created_date'::timestamptz,
    updated_at = COALESCE(selling_date, :'created_date'::timestamptz),
    transfer_started_at = CASE WHEN transfer_started_at IS NOT NULL
                               THEN :'created_date'::timestamptz END
WHERE sku_supplier = :'supplier_org'
  AND created_at > :'created_date'::timestamptz;

-- 2) Transfer/upload audit rows affecting the supplier.
UPDATE vault.transactions
SET ts = :'created_date'::timestamptz
WHERE :'supplier_org' = ANY (affected_orgs)
  AND ts > :'created_date'::timestamptz;

-- 3) Transfer/upload jobs owned by the supplier.
UPDATE vault.jobs
SET start_ts = :'created_date'::timestamptz,
    end_ts = CASE WHEN end_ts IS NOT NULL THEN :'created_date'::timestamptz END
WHERE organisation_id = :'supplier_org'
  AND start_ts > :'created_date'::timestamptz;

-- Sanity: after backdating, no sold key may predate its own creation.
\echo 'Post-check: sold-before-created keys (must be 0):'
SELECT count(*) AS sold_before_created
FROM vault.product_keys
WHERE sku_supplier = :'supplier_org'
  AND selling_date IS NOT NULL
  AND selling_date < created_at;

COMMIT;
