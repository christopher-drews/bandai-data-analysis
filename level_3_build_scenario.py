"""Build a LootVault scenario YAML: Bandai orgs, SKU catalog, SRP, and promotions.

Phase 4. Reads the enriched SKU list plus the SRP and promotion histories and
emits a scenario the lootvault CLI can apply (``scenario apply``):

  * orgs      — bandai (supplier), heybox, sonkwo (resellers).
  * skus[]    — every SKU; ``pa_pax_code`` when known; ``srp[]`` = the SKU's
                dated SRP windows (CNY) from level_1_extract_srp_history.
  * promotions[] — from level_1_extract_promo_history, with windows that share
                (start_date, end_date, discount, reseller-scope) grouped into one
                campaign covering many SKUs. ``Customer`` maps to a reseller scope
                (All -> every reseller; Heybox/Sonkwo -> that reseller). Alibaba
                rows are dropped (out of scope).

Hybrid build (see RUNBOOK.md): still **no keys and no sales** — the live-API leg
owns inventory + sales. ``steamId`` is not emitted (SkuSpec ``deny_unknown_fields``).
``cost`` is not emitted (the report has no cost; add a default later if wanted).

Output: data/level_3_build_scenario/bandai-skus.yaml
"""

from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date
from pathlib import Path

import pandas as pd

DEFAULT_SKUS = Path("data/level_2_enrich_pax_codes/skus_enriched.csv")
DEFAULT_SRP = Path("data/level_1_extract_srp_history/product_srp_history.csv")
DEFAULT_PROMO = Path("data/level_1_extract_promo_history/product_promo_history.csv")
DEFAULT_OUTPUT = Path("data/level_3_build_scenario/bandai-skus.yaml")

SCENARIO_NAME = "bandai-skus"
SUPPLIER = {"alias": "bandai", "name": "Bandai Namco", "role": "supplier"}
RESELLERS = [
    {"alias": "heybox", "name": "Heybox", "role": "reseller"},
    {"alias": "sonkwo", "name": "Sonkwo", "role": "reseller"},
]
BOOTSTRAP = {"email": "admin@example.com", "password": "test123"}

# Customer (report reseller) -> scenario reseller aliases. None => all resellers.
# Alibaba is intentionally absent: its rows are dropped.
CUSTOMER_SCOPE: dict[str, list[str] | None] = {
    "All": None,
    "Heybox": ["heybox"],
    "Sonkwo": ["sonkwo"],
}
RESELLER_DISPLAY = {"heybox": "Heybox", "sonkwo": "Sonkwo"}


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


def build_promotions(promo: pd.DataFrame, alias_set: set[str]) -> tuple[list[dict], dict[str, int]]:
    stats = {"dropped_alibaba": 0, "unknown_customer": 0, "unknown_sku": 0}
    groups: dict[tuple, list[str]] = {}
    for _, r in promo.iterrows():
        customer = r["Customer"]
        if customer == "Alibaba":
            stats["dropped_alibaba"] += 1
            continue
        if customer not in CUSTOMER_SCOPE:
            stats["unknown_customer"] += 1
            continue
        slug = r["Normalized Name"]
        if slug not in alias_set:
            stats["unknown_sku"] += 1
            continue
        resellers = CUSTOMER_SCOPE[customer]
        pct = fmt_num(float(r["Promo Discount"]) * 100)
        key = (r["start_date"], r["end_date"], pct, tuple(resellers) if resellers else None)
        groups.setdefault(key, []).append(slug)

    promotions: list[dict] = []
    for (sd, ed, pct, res), slugs in groups.items():
        label = "All" if not res else "+".join(RESELLER_DISPLAY.get(x, x) for x in res)
        promotions.append({
            "name": f"{sd} {label} {pct}%",
            "start_date": sd, "end_date": ed, "discount_percentage": pct,
            "resellers": list(res) if res else None,
            "skus": sorted(set(slugs)),
        })
    promotions.sort(key=lambda p: (p["start_date"], p["name"], p["skus"][0]))
    return promotions, stats


def build_yaml(skus: pd.DataFrame, srp_by_slug: dict[str, list[dict]], promotions: list[dict]) -> str:
    lines: list[str] = [
        "# Scenario: Bandai orgs + SKU catalog + SRP + promotions (generated — do not hand-edit).",
        "# Source: level_2_enrich_pax_codes + level_1_extract_{srp,promo}_history via level_3_build_scenario.py",
        "#",
        "# Hybrid build: no keys, no sales here (the live-API leg owns those).",
        "#",
        "# Apply (bandai cloud env):",
        "#   scripts/run_test_data_scenario_cloud.sh bandai <this-file>",
        "# Validate offline: lootvault_cli scenario validate --file <this-file>",
        f"name: {SCENARIO_NAME}",
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
        windows = srp_by_slug.get(r["Normalized Name"], [])
        if windows:
            lines.append("    srp:")
            for w in windows:
                lines.append(f"      - start_date: {w['start_date']}")
                if "end_date" in w:
                    lines.append(f"        end_date: {w['end_date']}")
                lines.append("        prices:")
                lines.append(f"          - {{ currency: {w['currency']}, price: {w['price']} }}")

    if promotions:
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
    parser.add_argument("--promo", default=DEFAULT_PROMO, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    df = pd.read_csv(args.skus, dtype=str, keep_default_na=False)
    all_skus = df.sort_values("Product Name").reset_index(drop=True)
    alias_set = set(all_skus["Normalized Name"])
    assert all_skus["Normalized Name"].is_unique, "SKU alias (Normalized Name) not unique"
    non_blank = all_skus.loc[all_skus["paxCode"] != "", "paxCode"]
    assert non_blank.is_unique, "paxCode not unique among emitted SKUs"

    srp = pd.read_csv(args.srp, dtype=str, keep_default_na=False)
    promo = pd.read_csv(args.promo, dtype=str, keep_default_na=False)
    srp_by_slug, srp_skipped = build_srp_by_slug(srp, alias_set)
    promotions, promo_stats = build_promotions(promo, alias_set)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_yaml(all_skus, srp_by_slug, promotions), encoding="utf-8")

    n_srp = sum(len(v) for v in srp_by_slug.values())
    print(f"Wrote {args.output}", file=sys.stderr)
    print(f"  orgs: {1 + len(RESELLERS)}  |  skus: {len(all_skus)} "
          f"({(all_skus['paxCode'] != '').sum()} with pa_pax_code)", file=sys.stderr)
    print(f"  SRP: {n_srp} windows across {len(srp_by_slug)} SKUs", file=sys.stderr)
    print(f"  promotions: {len(promotions)} campaigns "
          f"(dropped Alibaba rows: {promo_stats['dropped_alibaba']})", file=sys.stderr)
    if srp_skipped:
        print(f"  warn: {len(srp_skipped)} SRP slug(s) not in SKU set (skipped)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
