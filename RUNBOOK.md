# Bandai environment setup — runbook

Set up the **Bandai LootVault environment** with data extracted from the June 2026
royalty report, uploaded via the LootVault CLI's **scenario** (test-data) mechanism.

- **Source data:** `BNEPA_Royalty_Report_June 2026.xlsx` (in the `bandai-data-analysis`
  main checkout — a superset of the old May file; adds `2026-05` + `2026-06`, data
  now runs `2024-08 .. 2026-06`).
- **Target:** the `bandai` cloud env — `https://bandai.knoxkee.io/`
  (`lootvault-bandai-kv`, db-proxy port `5448`).
- **Upload path:** generate one scenario YAML, then
  `lootvault_cli scenario apply` (via `scripts/run_test_data_scenario_cloud.sh bandai`).

Two repos are involved:

| Repo | Role |
| --- | --- |
| `bandai-data-analysis` | Extract + transform the royalty report → **generate the scenario YAML** |
| `lootvault` | Owns the scenario schema (`src/bin/lootvault_cli/scenario/spec.rs`), the `scenario apply` CLI, and `scenarios/bandai-heybox-sonkwo.yaml` |

---

## Why the scenario path (not the `upload_*.py` scripts)

The `bandai-data-analysis` repo has a full set of live-API upload scripts
(`upload_skus.py`, `upload_srps.py`, `upload_promotions.py`,
`prepare_sales_upload.py`, `upload_sales_history.py`, …). Those POST directly to
`lv.play-asia.com` and require admin/backdating privileges.

The scenario path supersedes them for standing up a **whole environment at once**:
one declarative YAML describes orgs, catalog, pricing, franchises, relationships,
promotions, keys, and sales; `scenario apply` seeds it in dependency order, is
alias-based (server generates real ids), and has an offline `validate`. This is
the mechanism to use here.

---

## Data-fidelity map (report → scenario)

The scenario schema (`lootvault/src/bin/lootvault_cli/scenario/spec.rs`) is the
source of truth. What each royalty concept maps to:

| Concept | Scenario field | Fidelity | Source in this repo |
| --- | --- | --- | --- |
| Partners | `orgs` (bandai=supplier, heybox/sonkwo=reseller) | exact | fixed |
| SKU catalog | `skus[].name`, `.pa_pax_code` | exact (matched rows only) | `level_0_match_pax_codes` |
| Pricing / SRP | `skus[].srp[]` — **multiple dated windows** | **full history** | `level_1_extract_srp_history` |
| Supplier cost | `skus[].cost[].percentage` | **assumption** (not in report; default 70%) | — |
| Promotions | `promotions[]` (grouped by dates+discount+resellers → sku list) | exact | `level_1_extract_promo_history` |
| Relationships / FX | `relationships[]` (authorize, region-china, monthly CNY→USD) | exact | fixed + FX extract |
| Inventory | `skus[].keys`, `relationships[].distribute_keys` | derived (must cover sales) | from sales totals |
| **Sales** | `sales[]` — `percent_sold` per reseller per month | **⚠ APPROXIMATE** | `level_2_anonymize_sales_history` |

### ⚠ Sales are simulated in a scenario — so we split the job (DECIDED: hybrid)

`SalesSpec` only accepts `{ reseller, month, percent_sold }` — a single
sell-through **fraction per reseller per month**, sold across the reseller's SKUs
at random. It **cannot** express real per-SKU unit counts.

**Decision: hybrid, for unit-accurate sales.**

- **Scenario owns:** orgs, SKUs (name/paxCode/SRP history/cost), franchises,
  relationships (authorize + region + monthly FX), promotions.
- **Scenario does NOT own inventory or sales:** omit the `sales` section and set
  `keys: 0` / `distribute_keys: 0`. This avoids double-staging and double-counting.
- **Live-API leg owns inventory + sales:** after the scenario applies,
  `prepare_sales_upload.py` stages exact supplier keys and transfers per-reseller
  shares, then `upload_sales_history.py` reports the real per-SKU, per-month sales
  — both pointed at the bandai env (`--host bandai.knoxkee.io`).

> The live-API scripts resolve SKUs by `paPaxCode` (the scenario sets these) and
> resellers by **org id**. The bandai env's reseller org ids are **server-generated
> by the scenario**, so after apply we must fetch heybox/sonkwo's real ids and write
> a bandai-specific `customer_org_map.csv`, plus obtain a bearer token for the env.
> See Phase 6.

---

## Prerequisites (one-time, on the target env)

0. **Wipe the bandai env first** (DECIDED: full rebuild on an empty DB). Reset the
   `bandai` database to a clean, migrated state before applying, so the scenario
   and live-API legs seed with no duplicate-SRP/promo risk. Confirm the exact
   cloud-reset command in `lootvault/scripts` (`reset_db.sh` is the local form;
   the cloud form runs a drop/re-migrate through the `db-proxy.sh bandai` tunnel).
1. **`org-platform` root org must exist** in the bandai env — key upload records it
   as the key distributor and it cannot be created via the API. Bootstrap once:
   ```
   (in lootvault)  scripts/create_default_setup_cloud.sh bandai
   ```
2. **DB-proxy tunnel open** for the DB-direct superuser bootstrap (Postgres is
   VNet-only):
   ```
   (in pa-cloud-infra)  ./scripts/db-proxy.sh bandai      # forwards localhost:5448
   ```
3. **Azure CLI logged in** with read access to `lootvault-bandai-kv` (`az login`).
4. Partner orgs (bandai/heybox/sonkwo) — created **by the scenario itself**
   (`orgs` section), so no manual step. *(The old `upload_*.py` org IDs are a
   different, live-API environment and are irrelevant here.)*

> ⚠️ Seeding test data into a live environment is rarely what you want. Confirm
> `bandai` is the intended target and whether it should be **wiped first** or
> **topped up** (see Decisions).

---

## Phase-by-phase

### Phase 1 — Extract from the June workbook  *(parallelizable)*

Point the level-0 scripts at the June file (note the **space** in the filename;
their `DEFAULT_WORKBOOK` still says `MAY2026`, so override explicitly). Copy the
workbook into the working checkout first, or run from the main checkout.

Run in parallel — independent inputs:
```
python level_0_export_royalty_csvs.py --workbook "BNEPA_Royalty_Report_June 2026.xlsx"
python extract_bandai_products.py          # refresh bandai_products.csv from Playasia
```
🚦 **Gate:** `data/level_0_export_royalty_csvs/2026-05.csv` and `2026-06.csv` exist
with sane row counts (~432 / ~482).

### Phase 2 — Unique SKUs + PAX codes  *(accuracy-critical, split in two)*

**Part 1 — `level_1_extract_skus.py`** (offline, deterministic; reads the level_0
CSVs). Emits one row per unique SKU keyed on normalized Product Name, each with its
**Customer Reference** (report `Item Number`) chosen by temporal dominance.
```
python level_1_extract_skus.py
```
Corruption handling (DECIDED: auto-fix + flag):
- Valid codes = `^[EL]\d{5}$` (`E####`/`L####` families; both real). `0` and other
  malformed values are dropped.
- Per product, the code with the widest distinct-period coverage wins — auto-fixes
  transient 1-month errors (e.g. `digimon_survive` → E05471, drops a 1-month E03730).
- Two products sharing a canonical code are merged **only** if the code is in the
  curated `data/known_name_variants.csv` (`item_number,correct_name,note`) — the
  control surface for spelling merges (GOD EATER 3, SD GUNDAM …CROSS RAYS, THE
  IDOLM@STER…). Unlisted shared codes stay flagged.
- Outputs `data/level_1_extract_skus/{skus.csv, review.csv}`.

🚦 **Gate — the most important human review in the run.** Read
`review.csv` (`competing_item_numbers`, `shared_item_number`, `no_valid_item_number`).
Confirm the auto-picked Customer References and add any new spelling variants to
`known_name_variants.csv`, then re-run. Current June-2026 result: **172 unique SKUs,
all with a valid Customer Reference; 6 flagged** (all clear-dominant competing codes).

Then run the completeness guard — asserts every origin product survived into
`skus.csv` and reconciles dropped Item Numbers (exits non-zero on any real gap):
```
python check_skus_completeness.py
```

**Catalog fetch — `extract_bandai_products.py`** (network, run once). Dumps all
Playasia Bandai products to `data/bandai_catalog.csv` (`paxCode, label, steamId`).
The endpoint is behind Cloudflare; the allowlisted `playasia-auth/1.0` User-Agent
(already set) returns 200. June-2026: **672 products** (403 with a steamId).
```
python extract_bandai_products.py
```

**Part 2 — `level_2_enrich_pax_codes.py`** (offline). Associates the clean SKU list
(`skus.csv`) to `bandai_catalog.csv`, assigning each SKU a `paxCode` + `steamId` by
name (exact → fuzzy ≥0.95 → base-game). The report carries no paxCode/steamId, so
the join is name-based; steamId rides along as metadata (it's not unique in the
catalog, so it can't be the join key).
```
python level_2_enrich_pax_codes.py
```
Outputs `data/level_2_enrich_pax_codes/{skus_enriched.csv, unmatched.csv}`.
June-2026 result: **131/172 uniquely matched** (130 exact, 1 fuzzy). Flagged for
manual curation (DECIDED — keep paxCode↔SKU 1:1, no auto-share):
- **29 editions** (Deluxe/Ultimate/…) with no own catalog entry → base-game paxCode
  is already taken, so left **blank**.
- **12 unmatched** — ~8 resolvable (name/edition differences: Katamari, SPY×ANYA,
  Tales …Beyond the Dawn "Expansion (DLC)"), ~4 true catalog gaps (IDOLM@STER
  Starlit Season, Death Note Killer Within, Taiko Rhythm Festival, SRW Y Expansion).

🚦 **Gate — manual matching.** `skus_enriched.csv` **always lists all 172 SKUs**
(blank paxCode where unmatched). To resolve the 41, fill
**`data/sku_paxcode_overrides.csv`** — seeded on first run with one row per blank
SKU (`Normalized Name, Product Name, Customer Reference, reason, paxCode, steamId,
note`). Enter a `paxCode` (steamId auto-fills from the catalog; or set it manually
for true catalog gaps), then re-run `level_2_enrich_pax_codes.py`. A manual entry
becomes `pax_match_status = manual`, wins over any auto-match, and is never blanked
by uniqueness enforcement. The template is never overwritten once it exists.
Only paxCode-bearing SKUs become catalog entries downstream.

### Phase 3 — SRP + promotion history  *(temporal)*
Both read the level_0 CSVs and join paxCode/Customer Reference from
`skus_enriched.csv` (via `pax_lookup`, now re-pointed there); both fold merged
spelling variants onto their canonical SKU slug (`build_slug_alias_map`) so a SKU's
history stays continuous across a rename.
```
python level_1_extract_srp_history.py       # SRP windows  → scenario skus[].srp[]
python level_1_extract_promo_history.py      # promo runs   → scenario promotions[]
```
- **SRP** collapses consecutive same-price months into `[start_month, end_month]`
  runs; **each SKU's most-recent run is left open-ended** (blank `end_month`) so its
  last-known price is the active one — even a one-off/discontinued title keeps a
  current price instead of showing empty. June-2026: **316 runs over 184 SKUs**;
  66 SKUs changed price ≥once.
- **Promotions** use an **evidence-based duration model** (the report is monthly, so
  in-month dates are inferred):
  - *full month* — a discount is the only promo that month AND no non-promo sales →
    whole month; consecutive full months of the same discount merge into one span.
  - *partial* — non-promo sales coexisted → short window (`--default-days`, default
    **14**). *stacked* — ≥2 discounts in a month → packed back-to-back, each
    `min(default_days, month_days/K)` days.
  - Partial/stacked windows are anchored at the month start; windows never overlap
    within a (product, reseller) **by construction** (no more 15/16 boundary splits,
    no "complex conflict" cases).
  - Heybox+Sonkwo windows sharing (slug, discount, dates) collapse to `All`.
  June-2026: **2365 windows / 171 SKUs** (basis: 1150 full, 740 partial, 475 stacked;
  lengths 9–365 days). `basis` column records how each window was derived.
  **Alibaba** promo rows still present — drop at scenario mapping.

Sales extracts (separate leg):
```
python level_1_extract_sales_history.py      # sales rows
python level_2_anonymize_sales_history.py    # → per-(paxCode,customer,month) amounts
```

### Phase 4 — Generate the scenario YAML  *(`level_3_build_scenario.py`)*

Emits **three** scenario files from the enriched SKU list and the Phase-3 extracts.
The split is by idempotency: SRP and promotions are **create-only** (re-applying
duplicates them), so they're isolated from the idempotent base. All three share the
orgs + SKU skeleton (needed to resolve aliases per run). **Apply order: base → srp
→ promotions.**
```
python level_3_build_scenario.py     # -> data/level_3_build_scenario/{bandai-base,bandai-srp,bandai-promotions}.yaml
```
Depends on two more extracts: `level_1_extract_reseller_skus.py` (which reseller
carried each SKU → per-reseller authorization) and `level_0_extract_exchange_rates.py`
(monthly CNY→USD rates from the workbook).

Current output (June-2026, all validated):
- **`bandai-base.yaml`** — 3 orgs, 184 SKUs (136 with paxCode), **2 relationships**. Idempotent → safe to re-run.
- **`bandai-srp.yaml`** — 3 orgs, 184 SKUs with **316 SRP windows**. Create-only.
- **`bandai-promotions.yaml`** — 3 orgs, 184 SKUs, **1293 promotions**. Create-only, largest.
Mapping details:
- `skus[]`: every SKU; `pa_pax_code` when known; **`customer_reference`** = the
  report Item Number (172 set, unique per supplier; 12 blank→omitted);
  **`steam_app_id`** = catalog Steam App ID (136 set; `steam_type` defaults to `app`
  in the CLI); `srp[]` = the SKU's dated CNY windows (open-ended when `end_month`
  blank). `cost` NOT emitted (no cost in report).
  *(Requires the lootvault CLI change adding `customer_reference` + `steam_app_id`/
  `steam_type` to SkuSpec/ensure_sku — PR playasia/lootvault#1531. Both set at SKU
  create time, so they land via `bandai-base`, applied first.)*
- `relationships[]`: bandai→heybox and bandai→sonkwo. **`authorize_skus` is
  data-driven** — each reseller gets only the SKUs it actually carried (Heybox 170,
  Sonkwo 169; 159 shared). The 4 SKUs with no reseller-attributed data stay
  unauthorized. `allowed_regions: [region-china]`, `distribute_keys: 0` (hybrid),
  and 23 monthly `exchange_rates` (CNY→USD, from the workbook's rate cell).
- `promotions[]`: windows sharing (start_date, end_date, discount, reseller-scope)
  grouped into one campaign over many SKUs. `Customer` → scope: All → all resellers
  (field omitted); Heybox/Sonkwo → that reseller; **Alibaba dropped**. `discount_percentage`
  = report fraction ×100. Name = `"{start} {scope} {pct}%"`.
- **Hybrid**: no `keys`, no `sales` (live-API leg owns those). `pa_pax_code` optional,
  so uncurated SKUs are still emitted (and can carry SRP/promotions).
- `franchises` + `apply_franchises: true` (optional, mirrors existing scenario).
- `relationships[]`: bandai→heybox, bandai→sonkwo — `authorize_skus: all`,
  `allowed_regions: [region-china]`, monthly `exchange_rates` (CNY→USD) for
  `2024-08 .. 2026-06`, **`distribute_keys: 0`** (live leg transfers exact qty).
- `promotions[]`: group `product_promo_history.csv` by
  `(start_date, end_date, discount, resellers)` → one entry each with its sku
  alias list. Collapsed Heybox+Sonkwo rows (`Customer=All`) → both resellers.
- **`sales[]`: omitted** — handled unit-accurately by the live-API leg (Phase 6).

🚦 **Gate:** offline validate (no server, no writes):
```
(in lootvault)  cargo run -r --bin lootvault_cli -- scenario validate \
                  --file scenarios/bandai-heybox-sonkwo.yaml
```
Catches duplicate aliases, unknown org refs, empty SRP windows, bad roles.

### Phase 5 — Apply to the bandai env
Prereqs (cloud runner): `az login`; db-proxy tunnel up (`pa-cloud-infra ./scripts/db-proxy.sh bandai`,
localhost:5448); `USER_KEY_SECRET` exported = the deployed bandai server's value.
Apply the three files **in order** (base → srp → promotions):
```
(in lootvault)  scripts/run_test_data_scenario_cloud.sh bandai .../bandai-base.yaml
                scripts/run_test_data_scenario_cloud.sh bandai .../bandai-srp.yaml
                scripts/run_test_data_scenario_cloud.sh bandai .../bandai-promotions.yaml
```
`base` is idempotent (re-run anytime). `srp`/`promotions` are create-only — apply once
per fresh (or per-aspect-cleared) env, else they duplicate. `org-platform` is NOT
required for this scenario (no keys/sales).
Seeds, in order: superuser (DB-direct) → orgs → exchange rates → SKUs (SRP,
cost) → franchises → relationships (authorize + FX + distribute keys) → supplier
keys → promotions → sales reports.

### Phase 6 — Unit-accurate sales, live-API leg (hybrid)
Runs **after** the scenario has seeded catalog + relationships. First bridge the
server-generated ids into the live-API scripts:
1. Obtain a bearer token for the bandai env (log in as the scenario superuser /
   a bandai supplier admin).
2. Fetch heybox/sonkwo real org ids from the env and write a bandai-specific
   `data/customer_org_map.csv` (`name,org_id`).
3. Stage + transfer exact inventory, then report real sales — both against the
   bandai host:
   ```
   python prepare_sales_upload.py  --host bandai.knoxkee.io --org-id <bandai-supplier-id> --token <JWT>
   python upload_sales_history.py  --host bandai.knoxkee.io --org-id <bandai-supplier-id> --token <JWT>
   ```
   (Both are resumable and honor `--dry-run`; `reconcile_sales_inventory.py` is the check.)

### Phase 7 — Verify
- Log into `https://bandai.knoxkee.io/` as the scenario superuser; spot-check
  catalog size, a few SKUs' SRP history, promotions, and monthly sales reports
  (unit counts should now match the report).

---

## Parallelism summary

| Run together | Why |
| --- | --- |
| `export_royalty_csvs` + `extract_bandai_products` | no shared inputs |
| the three `level_1_extract_*` scripts | independent, same upstream |
| Everything after that | sequential: match → generate → validate → apply |

---

## Decisions

1. **Sales fidelity → HYBRID.** Scenario seeds catalog/SRP/promos/relationships
   (no inventory, no sales); live-API leg replays unit-accurate sales (Phase 6).
2. **Wipe vs. top-up → WIPE FIRST.** Full rebuild on a clean, migrated bandai DB
   (Phase 0).

Still to confirm:

3. **Supplier cost %** — report has no cost; default 70% of SRP unless told otherwise.
4. **Alibaba** — present in `data/customer_org_map.csv` but out of scope (3 partners:
   bandai/heybox/sonkwo). Confirm it stays excluded.

---

## Command appendix

```bash
# --- bandai-data-analysis (extraction) ---
python level_0_export_royalty_csvs.py --workbook "BNEPA_Royalty_Report_June 2026.xlsx"
python extract_bandai_products.py
python level_0_match_pax_codes.py    --workbook "BNEPA_Royalty_Report_June 2026.xlsx"
python level_1_extract_srp_history.py
python level_1_extract_promo_history.py
python level_1_extract_sales_history.py
python level_2_anonymize_sales_history.py
python level_3_build_scenario.py     # TO BUILD → scenarios/bandai-heybox-sonkwo.yaml

# --- lootvault (wipe + validate + apply) ---
# 0. wipe bandai DB to a clean migrated state (confirm exact cloud-reset cmd)
# (in pa-cloud-infra, separate shell) ./scripts/db-proxy.sh bandai
scripts/create_default_setup_cloud.sh bandai            # one-time: org-platform
cargo run -r --bin lootvault_cli -- scenario validate --file scenarios/bandai-heybox-sonkwo.yaml
scripts/run_test_data_scenario_cloud.sh bandai scenarios/bandai-heybox-sonkwo.yaml

# --- bandai-data-analysis (hybrid sales leg, after scenario apply) ---
# fetch heybox/sonkwo org ids from the env -> data/customer_org_map.csv, get a JWT, then:
python prepare_sales_upload.py --host bandai.knoxkee.io --org-id <bandai-supplier-id> --token <JWT>
python upload_sales_history.py --host bandai.knoxkee.io --org-id <bandai-supplier-id> --token <JWT>
```
