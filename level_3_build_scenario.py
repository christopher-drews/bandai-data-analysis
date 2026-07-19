"""Build a LootVault scenario YAML: Bandai orgs, SKU catalog, SRP, and promotions.

Phase 4. Reads the enriched SKU list plus the SRP and promotion histories and
emits a scenario the lootvault CLI can apply (``scenario apply``):

  * orgs      — bandai (supplier), heybox, sonkwo (resellers).
  * skus[]    — every SKU; ``pa_pax_code`` when known; ``srp[]`` = the SKU's
                dated SRP windows (CNY) from level_1_extract_srp_history.
  * promotions[] — read verbatim from level_2_build_promotion_campaigns
                (already grouped into campaigns and named); emitted as-is.

Hybrid build (see RUNBOOK.md): still **no keys and no sales** — the live-API leg
owns inventory + sales. ``steamId`` is not emitted (SkuSpec ``deny_unknown_fields``).
``cost`` is not emitted (the report has no cost; add a default later if wanted).

Emits three scenario files (SRP and promotions are create-only, so re-applying
duplicates them; the base is idempotent). All three share the orgs + SKU skeleton
(needed to resolve aliases per run). Apply in order: base, srp, promotions.
  - data/level_3_build_scenario/bandai-base.yaml        orgs + skus + relationships
  - data/level_3_build_scenario/bandai-srp.yaml         orgs + skus (with srp[])
  - data/level_3_build_scenario/bandai-promotions.yaml  orgs + skus + promotions
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

DEFAULT_SKUS = Path("data/level_2_enrich_pax_codes/skus_enriched.csv")
DEFAULT_SRP = Path("data/level_1_extract_srp_history/product_srp_history.csv")
DEFAULT_CAMPAIGNS = Path("data/level_2_build_promotion_campaigns/promotion_campaigns.json")
DEFAULT_RESELLER_SKUS = Path("data/level_1_extract_reseller_skus/reseller_skus.csv")
DEFAULT_FX = Path("data/level_0_extract_exchange_rates/exchange_rates.csv")
DEFAULT_OUTPUT_DIR = Path("data/level_3_build_scenario")

RESELLER_ALIAS = {"Heybox": "heybox", "Sonkwo": "sonkwo"}
REGION = "region-china"

SCENARIO_NAME = "bandai-skus"
SUPPLIER = {"alias": "bandai", "name": "Bandai Namco", "role": "supplier"}
RESELLERS = [
    {"alias": "heybox", "name": "Heybox", "role": "reseller"},
    {"alias": "sonkwo", "name": "Sonkwo", "role": "reseller"},
]
BOOTSTRAP = {"email": "admin@example.com", "password": "test123"}


def yq(s: str) -> str:
    """YAML double-quoted scalar (safe for ':', '@', '×', '™', quotes, etc.)."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def fmt_num(x: float, nd: int = 4) -> str:
    """Trim a number for YAML: integer when whole, else rounded to nd places."""
    v = round(float(x), nd)
    return str(int(v)) if v == int(v) else str(v)


def month_first(ym: str) -> date:
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, 1)


def month_last(ym: str) -> date:
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, calendar.monthrange(y, m)[1])


def build_srp_by_slug(srp: pd.DataFrame, alias_set: set[str]) -> tuple[dict[str, list[dict]], list[str]]:
    by_slug: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for _, r in srp.iterrows():
        slug = r["Normalized Name"]
        if slug not in alias_set:
            skipped.append(slug)
            continue
        window = {
            "start_date": month_first(r["start_month"]).isoformat(),
            "currency": (r["currency"].strip() or "CNY"),
            "price": fmt_num(r["SRP"], 2),
        }
        if r["end_month"].strip():
            window["end_date"] = month_last(r["end_month"]).isoformat()
        by_slug.setdefault(slug, []).append(window)
    for windows in by_slug.values():
        windows.sort(key=lambda w: w["start_date"])
    return by_slug, sorted(set(skipped))


def build_fx_rates(fx: pd.DataFrame) -> list[dict]:
    rates = []
    for _, r in fx.sort_values("month").iterrows():
        y, m = r["month"].split("-")
        rates.append({"year": int(y), "month": int(m), "rate": fmt_num(r["exchange_rate"], 5)})
    return rates


def build_relationships(reseller_skus: pd.DataFrame, fx: pd.DataFrame, alias_set: set[str]) -> list[dict]:
    """One relationship per reseller: authorize only the SKUs it actually carried."""
    fx_rates = build_fx_rates(fx)
    rels = []
    for display, alias in RESELLER_ALIAS.items():
        skus = sorted(
            s for s in reseller_skus.loc[reseller_skus["reseller"] == display, "Normalized Name"]
            if s in alias_set
        )
        rels.append({"target": alias, "authorize_skus": skus, "fx": fx_rates})
    return rels


def build_yaml(name: str, skus: pd.DataFrame, srp_by_slug: dict[str, list[dict]],
               relationships: list[dict], promotions: list[dict],
               *, include_srp: bool, include_relationships: bool, include_promotions: bool) -> str:
    """Render one scenario file. The orgs + SKU skeleton is always emitted (needed to
    resolve aliases per run); the payload sections are gated by the include_* flags.
    """
    lines: list[str] = [
        f"# Scenario '{name}' (generated by level_3_build_scenario.py — do not hand-edit).",
        "# Split into base / srp / promotions because SRP and promotions are create-only",
        "# (re-applying duplicates them); the base is idempotent. Apply order: base, srp,",
        "# promotions. Hybrid build: no keys, no sales (the live-API leg owns those).",
        "#",
        "# Apply (bandai cloud env): scripts/run_test_data_scenario_cloud.sh bandai <this-file>",
        "# Validate offline:         lootvault_cli scenario validate --file <this-file>",
        f"name: {name}",
        "",
        "bootstrap:",
        "  superuser:",
        f"    email: {yq(BOOTSTRAP['email'])}",
        f"    password: {yq(BOOTSTRAP['password'])}",
        "",
        "orgs:",
    ]
    for org in [SUPPLIER, *RESELLERS]:
        lines.append(f"  - {{ alias: {org['alias']}, name: {yq(org['name'])}, role: {org['role']} }}")

    lines += ["", "skus:"]
    for _, r in skus.iterrows():
        lines.append(f"  - supplier: {SUPPLIER['alias']}")
        lines.append(f"    alias: {yq(r['Normalized Name'])}")
        lines.append(f"    name: {yq(r['Product Name'])}")
        if r["paxCode"]:
            lines.append(f"    pa_pax_code: {r['paxCode']}")
        if r["Customer Reference"]:
            lines.append(f"    customer_reference: {yq(r['Customer Reference'])}")
        if r.get("steamId"):
            # steam_type defaults to "app" in the CLI (these are Steam App IDs).
            lines.append(f"    steam_app_id: {yq(r['steamId'])}")
        windows = srp_by_slug.get(r["Normalized Name"], []) if include_srp else []
        if windows:
            lines.append("    srp:")
            for w in windows:
                lines.append(f"      - start_date: {w['start_date']}")
                if "end_date" in w:
                    lines.append(f"        end_date: {w['end_date']}")
                lines.append("        prices:")
                lines.append(f"          - {{ currency: {w['currency']}, price: {w['price']} }}")

    if include_relationships and relationships:
        lines += ["", "relationships:"]
        for rel in relationships:
            lines.append(f"  - source: {SUPPLIER['alias']}")
            lines.append(f"    target: {rel['target']}")
            lines.append(f"    authorize_skus: [{', '.join(yq(a) for a in rel['authorize_skus'])}]")
            lines.append(f"    allowed_regions: [{REGION}]")
            lines.append("    distribute_keys: 0")
            lines.append("    exchange_rates:")
            for fr in rel["fx"]:
                lines.append(
                    f"      - {{ year: {fr['year']}, month: {fr['month']}, "
                    f"rates: [ {{ currency: CNY, rate_to_usd: {fr['rate']} }} ] }}"
                )

    if include_promotions and promotions:
        lines += ["", "promotions:"]
        for p in promotions:
            lines.append(f"  - org: {SUPPLIER['alias']}")
            lines.append(f"    name: {yq(p['name'])}")
            lines.append(f"    start_date: {p['start_date']}")
            lines.append(f"    end_date: {p['end_date']}")
            lines.append(f"    discount_percentage: {p['discount_percentage']}")
            if p["resellers"]:
                lines.append(f"    resellers: [{', '.join(p['resellers'])}]")
            lines.append(f"    skus: [{', '.join(yq(a) for a in p['skus'])}]")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skus", default=DEFAULT_SKUS, type=Path)
    parser.add_argument("--srp", default=DEFAULT_SRP, type=Path)
    parser.add_argument("--campaigns", default=DEFAULT_CAMPAIGNS, type=Path)
    parser.add_argument("--reseller-skus", default=DEFAULT_RESELLER_SKUS, type=Path)
    parser.add_argument("--fx", default=DEFAULT_FX, type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
    args = parser.parse_args()

    df = pd.read_csv(args.skus, dtype=str, keep_default_na=False)
    all_skus = df.sort_values("Product Name").reset_index(drop=True)
    alias_set = set(all_skus["Normalized Name"])
    assert all_skus["Normalized Name"].is_unique, "SKU alias (Normalized Name) not unique"
    non_blank = all_skus.loc[all_skus["paxCode"] != "", "paxCode"]
    assert non_blank.is_unique, "paxCode not unique among emitted SKUs"

    srp = pd.read_csv(args.srp, dtype=str, keep_default_na=False)
    promotions = json.loads(args.campaigns.read_text(encoding="utf-8"))
    reseller_skus = pd.read_csv(args.reseller_skus, dtype=str, keep_default_na=False)
    fx = pd.read_csv(args.fx, dtype=str, keep_default_na=False)
    srp_by_slug, srp_skipped = build_srp_by_slug(srp, alias_set)
    relationships = build_relationships(reseller_skus, fx, alias_set)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "bandai-base": dict(include_srp=False, include_relationships=True, include_promotions=False),
        "bandai-srp": dict(include_srp=True, include_relationships=False, include_promotions=False),
        "bandai-promotions": dict(include_srp=False, include_relationships=False, include_promotions=True),
    }
    for name, flags in files.items():
        path = args.output_dir / f"{name}.yaml"
        path.write_text(
            build_yaml(name, all_skus, srp_by_slug, relationships, promotions, **flags),
            encoding="utf-8",
        )
        print(f"Wrote {path}", file=sys.stderr)

    n_srp = sum(len(v) for v in srp_by_slug.values())
    print(f"\n  common skeleton: {1 + len(RESELLERS)} orgs, {len(all_skus)} skus "
          f"({(all_skus['paxCode'] != '').sum()} with pa_pax_code)", file=sys.stderr)
    print(f"  base       -> relationships: {len(relationships)} "
          f"({', '.join(r['target'] + '=' + str(len(r['authorize_skus'])) for r in relationships)})", file=sys.stderr)
    print(f"  srp        -> {n_srp} SRP windows across {len(srp_by_slug)} SKUs", file=sys.stderr)
    print(f"  promotions -> {len(promotions)} campaigns "
          f"(from {args.campaigns})", file=sys.stderr)
    if srp_skipped:
        print(f"  warn: {len(srp_skipped)} SRP slug(s) not in SKU set (skipped)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
