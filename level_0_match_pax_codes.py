"""Match distinct royalty products to the Bandai PAX catalog.

Loads every monthly sheet of BNEPA_Royalty_Report_*.xlsx, builds the
distinct list of products by Normalized Name, then merges against the
Bandai catalog (data/bandai_products.csv) in three passes:

  1. exact ``Normalized Name`` join
  2. SequenceMatcher fuzzy match (>= 0.95)
  3. base-game fallback that strips edition qualifiers
     (e.g. "tekken_8_deluxe" -> "tekken_8")

Writes data/level_0_match_pax_codes/royalty_pax_match.csv with one row
per royalty product (Normalized Name unique) and its assigned paxCode.
A paxCode is assigned to at most one row — for any subsequent product
that would re-use the same code, the paxCode is moved into the
``related pax code`` column instead. Extracted from cell 23 of
royalty_analysis.ipynb.
"""

from __future__ import annotations

import argparse
import sys
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

from normalize import normalize_name

DEFAULT_WORKBOOK = Path("BNEPA_Royalty_Report_MAY2026_LV.xlsx")
DEFAULT_BANDAI = Path("data/bandai_products.csv")
DEFAULT_PAX_XLSX = Path("royalty_pax_match.xlsx")
DEFAULT_OUTPUT = Path("data/level_0_match_pax_codes/royalty_pax_match.csv")

SKIP_SHEETS = {"New template", "銷售庫存統計總表"}
HEADER_SCAN_DEPTH = 12
SIMILARITY_THRESHOLD = 0.95

# Longer multi-token qualifiers first so e.g. "tekken_8_season_2_deluxe"
# strips "_deluxe" before "_season_2".
QUALIFIER_SUFFIXES = (
    "season_2_deluxe", "season_2_ultimate",
    "season_3_deluxe", "season_3_ultimate",
    "season_2", "season_3",
    "deluxe", "ultimate", "special", "master", "premium",
    "legendary", "platinum", "daima", "advanced", "gold",
)


def find_header_row(path: Path, sheet: str, depth: int = HEADER_SCAN_DEPTH) -> int:
    probe = pd.read_excel(path, sheet_name=sheet, header=None, nrows=depth, engine="openpyxl")
    for i in range(len(probe)):
        cells = {str(v).strip() for v in probe.iloc[i].tolist() if pd.notna(v)}
        if "Product Name" in cells:
            return i
    raise ValueError(f"No 'Product Name' header found in first {depth} rows of {sheet!r}")


def load_distinct_products(workbook: Path) -> pd.DataFrame:
    """One row per Normalized Name, taking the most recent sheet's spelling."""
    xl = pd.ExcelFile(workbook, engine="openpyxl")
    monthly = [s for s in xl.sheet_names if s not in SKIP_SHEETS]

    parts: list[pd.DataFrame] = []
    for sheet in monthly:
        hdr = find_header_row(workbook, sheet)
        df = pd.read_excel(xl, sheet_name=sheet, header=hdr, engine="openpyxl")
        cols = {c.strip(): c for c in df.columns if isinstance(c, str)}
        pn = cols.get("Product Name")
        inum = cols.get("Item Number")
        if not pn or not inum:
            print(f"  skip {sheet!r}: missing Product Name / Item Number", file=sys.stderr)
            continue
        sub = df[[pn, inum]].rename(columns={pn: "Product Name", inum: "Item Number"})
        sub["sheet"] = sheet
        parts.append(sub)

    combined = pd.concat(parts, ignore_index=True)
    combined = combined.dropna(subset=["Product Name", "Item Number"], how="any")
    combined["Product Name"] = combined["Product Name"].astype(str).str.strip()
    combined["Item Number"] = combined["Item Number"].astype(str).str.strip()
    combined = combined[(combined["Product Name"] != "") & (combined["Item Number"] != "")]
    combined["Normalized Name"] = combined["Product Name"].map(normalize_name)
    combined = combined[combined["Normalized Name"] != ""]

    # `monthly` is chronological, so sheet position is a recency rank.
    sheet_rank = {s: i for i, s in enumerate(monthly)}
    combined["_rank"] = combined["sheet"].map(sheet_rank)

    distinct = (
        combined.sort_values("_rank")
        .drop_duplicates(subset=["Normalized Name"], keep="last")[
            ["Product Name", "Item Number", "Normalized Name"]
        ]
        .sort_values(["Product Name", "Item Number"])
        .reset_index(drop=True)
    )
    assert distinct["Normalized Name"].is_unique, "Normalized Name column is not unique"
    return distinct


def find_base_game_match(slug: str, bandai_set: set[str]) -> tuple[str, list[str]] | None:
    """Strip qualifier suffixes from ``slug`` until it matches a Bandai slug."""
    current, stripped = slug, []
    while current:
        if current in bandai_set:
            return current, stripped
        matched = False
        for qual in QUALIFIER_SUFFIXES:
            if current.endswith(f"_{qual}"):
                current = current[: -(len(qual) + 1)]
                stripped.append(qual)
                matched = True
                break
        if not matched:
            return None
    return None


def match(distinct: pd.DataFrame, bandai: pd.DataFrame) -> pd.DataFrame:
    merged = distinct.merge(
        bandai, on="Normalized Name", how="outer", indicator="match_status"
    )
    merged["match_status"] = merged["match_status"].map({
        "both": "matched",
        "left_only": "distinct_only",
        "right_only": "bandai_only",
    })
    merged["lookup_score"] = pd.NA
    merged["lookup_bandai_slug"] = pd.NA

    bandai_slugs = bandai["Normalized Name"].dropna().unique().tolist()
    bandai_slug_set = set(bandai_slugs)
    bandai_by_slug = bandai.groupby("Normalized Name")[["paxCode", "label"]]

    unmatched = (
        merged[merged["match_status"] == "distinct_only"]
        [["Normalized Name", "Product Name", "Item Number"]]
        .drop_duplicates(subset=["Normalized Name"])
    )

    # Pass 1: SequenceMatcher similarity (>= threshold).
    similarity_rows: list[dict] = []
    for _, row in unmatched.iterrows():
        slug = row["Normalized Name"]
        if not isinstance(slug, str) or not slug:
            continue
        best, best_score = None, 0.0
        for cand in bandai_slugs:
            score = SequenceMatcher(None, slug, cand).ratio()
            if score > best_score:
                best, best_score = cand, score
        if best is None or best_score < SIMILARITY_THRESHOLD:
            continue
        for _, bandai_row in bandai_by_slug.get_group(best).iterrows():
            similarity_rows.append({
                "Normalized Name": slug,
                "Product Name": row["Product Name"],
                "Item Number": row["Item Number"],
                "paxCode": bandai_row["paxCode"],
                "label": bandai_row["label"],
                "match_status": "similarity_match",
                "lookup_score": round(best_score, 3),
                "lookup_bandai_slug": best,
            })

    # Pass 2: base-game fallback for Edition-suffix slugs not picked up above.
    promoted_by_similarity = {r["Normalized Name"] for r in similarity_rows}
    base_game_rows: list[dict] = []
    for _, row in unmatched.iterrows():
        slug = row["Normalized Name"]
        if slug in promoted_by_similarity:
            continue
        if not isinstance(slug, str) or not slug:
            continue
        result = find_base_game_match(slug, bandai_slug_set)
        if result is None:
            continue
        base_slug, _stripped = result
        for _, bandai_row in bandai_by_slug.get_group(base_slug).iterrows():
            base_game_rows.append({
                "Normalized Name": slug,
                "Product Name": row["Product Name"],
                "Item Number": row["Item Number"],
                "paxCode": bandai_row["paxCode"],
                "label": bandai_row["label"],
                "match_status": "base_game_match",
                "lookup_score": pd.NA,
                "lookup_bandai_slug": base_slug,
            })

    promoted_all = promoted_by_similarity | {r["Normalized Name"] for r in base_game_rows}
    if promoted_all:
        keep_mask = ~(
            (merged["match_status"] == "distinct_only")
            & (merged["Normalized Name"].isin(promoted_all))
        )
        merged = pd.concat(
            [merged[keep_mask], pd.DataFrame(similarity_rows + base_game_rows)],
            ignore_index=True,
        )

    merged = merged[
        ["Normalized Name", "Product Name", "Item Number", "paxCode", "label",
         "match_status", "lookup_score", "lookup_bandai_slug"]
    ]
    # Drop bandai_only — the output is the royalty catalog with its assigned
    # matches, not the full Bandai catalog.
    merged = merged[merged["match_status"] != "bandai_only"].reset_index(drop=True)
    return enforce_uniqueness(merged)


# Higher-quality matches claim their paxCode first; weaker matches that
# would re-use the same code get demoted to ``related pax code``.
MATCH_PRIORITY = {
    "matched": 0,
    "similarity_match": 1,
    "base_game_match": 2,
    "distinct_only": 3,
}


def enforce_uniqueness(merged: pd.DataFrame) -> pd.DataFrame:
    """Ensure Normalized Name and paxCode are each unique in the output.

    - Duplicate Normalized Name rows: keep the first (in priority order).
    - Duplicate paxCode rows: blank ``paxCode`` and move the value into a
      new ``related pax code`` column.
    """
    df = merged.copy()
    df["_priority"] = df["match_status"].map(MATCH_PRIORITY).fillna(99)
    df = df.sort_values(["_priority", "Normalized Name"]).reset_index(drop=True)
    df = df.drop_duplicates(subset=["Normalized Name"], keep="first").reset_index(drop=True)

    df["related pax code"] = pd.NA
    seen: set[str] = set()
    for i, pax in enumerate(df["paxCode"]):
        if pd.isna(pax) or str(pax).strip() == "":
            continue
        if pax in seen:
            df.at[i, "related pax code"] = pax
            df.at[i, "paxCode"] = pd.NA
        else:
            seen.add(pax)

    df = df.drop(columns="_priority")
    return df[
        ["Normalized Name", "Product Name", "Item Number", "paxCode",
         "related pax code", "label", "match_status", "lookup_score",
         "lookup_bandai_slug"]
    ].sort_values(["match_status", "Normalized Name"]).reset_index(drop=True)


def load_customer_references(xlsx_path: Path) -> dict[str, str]:
    """Normalized Name -> Customer Reference from the hand-curated SKUs sheet.

    Last-write wins on duplicate slugs.
    """
    df = pd.read_excel(xlsx_path, sheet_name="SKUs", engine="openpyxl")
    df = df[["Name", "Customer Reference"]].copy()
    df["slug"] = df["Name"].map(normalize_name)
    df = df[df["slug"] != ""]
    lookup: dict[str, str] = {}
    for _, row in df.iterrows():
        ref = row["Customer Reference"] if pd.notna(row["Customer Reference"]) else ""
        lookup[row["slug"]] = ref
    return lookup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", default=DEFAULT_WORKBOOK, type=Path)
    parser.add_argument("--bandai", default=DEFAULT_BANDAI, type=Path)
    parser.add_argument("--pax-xlsx", default=DEFAULT_PAX_XLSX, type=Path,
                        help="Hand-curated SKU sheet supplying Customer Reference.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    args = parser.parse_args()

    distinct = load_distinct_products(args.workbook)
    print(f"{len(distinct)} distinct royalty products", file=sys.stderr)

    bandai = pd.read_csv(args.bandai, dtype=str)
    if "Normalized Name" not in bandai.columns:
        bandai["Normalized Name"] = bandai["label"].map(normalize_name)
    print(f"{len(bandai)} Bandai catalog rows", file=sys.stderr)

    result = match(distinct, bandai)

    customer_refs = load_customer_references(args.pax_xlsx)
    result["Customer Reference"] = result["Normalized Name"].map(customer_refs).fillna("")
    print(f"{len(customer_refs)} Customer Reference entries loaded", file=sys.stderr)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    print(f"\nWrote {args.output} ({len(result)} rows)", file=sys.stderr)

    counts = result["match_status"].value_counts().to_dict()
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}", file=sys.stderr)
    related = result["related pax code"].notna().sum()
    print(f"  related pax code: {related}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
