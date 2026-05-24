# Project rules

## Script naming + output layout

Pipeline scripts are named `level_<N>_<action>.py`, where `<N>` is the data-flow depth:

- `level_0_<action>.py` — executes on the original source data (e.g., the raw `BNEPA_Royalty_Report_*.xlsx`).
- `level_1_<action>.py` — executes on the outputs of `level_0_*` scripts.
- `level_2_<action>.py` — executes on the outputs of `level_1_*` scripts.
- …and so on.

Each script emits one or more CSVs to its own output directory:

- Source file: `level_<N>_<action>.py` at the repo root.
- Output directory: `data/level_<N>_<action>/` (matches the script's basename without `.py`).
- Output files: `*.csv` — one CSV per logical unit (e.g., per period, per category).
- Filenames should encode the unit they represent (e.g., `2026-05.csv`).
- The script creates `data/<script_basename>/` if it does not exist.
- Downstream (`level_<N+1>_*`) scripts read from `data/level_<N>_<action>/` directories — never from the repo root.

Existing aggregator scripts (`extract_*.py`) follow a different convention: they emit a single CSV at the repo root and are not subject to this rule.
