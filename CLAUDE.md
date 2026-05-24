# Project rules

## Script naming + output layout

Scripts named `base_<action>.py` are bulk extractors that emit multiple CSVs to a per-script output directory:

- Source file: `base_<action>.py` at the repo root.
- Output directory: `data/base_<action>/` (the directory name matches the script's basename without `.py`).
- Output files: `*.csv` — one CSV per logical unit (e.g., per period, per category).
- Filenames should encode the unit they represent (e.g., `2026-05.csv`).
- The script creates `data/<script_basename>/` if it does not exist.

Existing aggregator scripts (`extract_*.py`) follow a different convention: they emit a single CSV at the repo root and are not subject to this rule.
