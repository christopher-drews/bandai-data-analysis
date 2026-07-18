"""Build a LootVault scenario YAML that creates the Bandai orgs + SKU catalog.

Phase 4 (scoped to SKUs). Reads the enriched SKU list from
data/level_2_enrich_pax_codes/skus_enriched.csv and emits a scenario the
lootvault CLI can apply (``scenario apply``): the three partner orgs plus **every**
SKU.

Hybrid build (see RUNBOOK.md): this file carries **no keys and no sales** — the
live-API leg owns inventory + sales. SRP, cost, promotions, and relationships are
added by later phases; this step only stands up the catalog.

**All SKUs are emitted.** ``pa_pax_code`` is optional in SkuSpec, so a SKU still
awaiting curation is written without one (it can be filled later via
sku_paxcode_overrides.csv and regenerated). ``steamId`` is deliberately omitted:
SkuSpec uses ``deny_unknown_fields``, so an extra key would fail to parse; the Steam
ids live in the enriched CSV for our own use.

Output: data/level_3_build_scenario/bandai-skus.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

DEFAULT_SKUS = Path("data/level_2_enrich_pax_codes/skus_enriched.csv")
DEFAULT_OUTPUT = Path("data/level_3_build_scenario/bandai-skus.yaml")

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


def build_yaml(skus: pd.DataFrame) -> str:
    lines: list[str] = [
        "# Scenario: Bandai orgs + SKU catalog (generated — do not hand-edit).",
        "# Source: data/level_2_enrich_pax_codes/skus_enriched.csv via level_3_build_scenario.py",
        "#",
        "# Hybrid build: no keys, no sales here (the live-API leg owns those). SRP/cost/",
        "# promotions/relationships are added by later phases.",
        "#",
        "# Apply (bandai cloud env):",
        "#   scripts/run_test_data_scenario_cloud.sh bandai <this-file>",
        "# Validate offline (no server):",
        "#   lootvault_cli scenario validate --file <this-file>",
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
        lines.append(
            f"  - {{ alias: {org['alias']}, name: {yq(org['name'])}, role: {org['role']} }}"
        )
    lines += ["", "skus:"]
    for _, r in skus.iterrows():
        entry = (
            f"  - {{ supplier: {SUPPLIER['alias']}, "
            f"alias: {yq(r['Normalized Name'])}, "
            f"name: {yq(r['Product Name'])}"
        )
        # pa_pax_code is optional; emit it only when the SKU has one.
        if r["paxCode"]:
            entry += f", pa_pax_code: {r['paxCode']}"
        lines.append(entry + " }")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skus", default=DEFAULT_SKUS, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    df = pd.read_csv(args.skus, dtype=str, keep_default_na=False)
    # Emit EVERY SKU (pa_pax_code is optional); sort for stable output.
    all_skus = df.sort_values("Product Name").reset_index(drop=True)
    with_pax = all_skus[all_skus["paxCode"] != ""]

    # Guard the invariants the scenario validator also enforces.
    assert all_skus["Normalized Name"].is_unique, "SKU alias (Normalized Name) not unique"
    non_blank = all_skus.loc[all_skus["paxCode"] != "", "paxCode"]
    assert non_blank.is_unique, "paxCode not unique among emitted SKUs"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_yaml(all_skus), encoding="utf-8")

    print(f"Wrote {args.output}", file=sys.stderr)
    print(f"  orgs: {1 + len(RESELLERS)}  |  skus: {len(all_skus)}", file=sys.stderr)
    print(f"  with pa_pax_code: {len(with_pax)}  |  without (awaiting curation): {len(all_skus) - len(with_pax)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
