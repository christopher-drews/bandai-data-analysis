"""Single source of truth for product-name normalization.

Used by ``royalty_analysis.ipynb`` and ``lookup_pax_codes.py`` so the
royalty report and Bandai catalog match on a common slug.
"""

from __future__ import annotations

import re

_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")
_DLC_SUFFIX_RE = re.compile(r"\s*\(\s*dlc\s*\)\s*$", re.IGNORECASE)
_EDITION_WORD_RE = re.compile(r"\bedition\b", re.IGNORECASE)
# "Noise" tokens that vary between the royalty report and the Bandai catalog
# but don't change which SKU is meant. Stripped as whole words so e.g.
# "Pre-Purchase X Pre-Order" collapses to "X".
_NOISE_RES = (
    re.compile(r"\bpre[ _-]?purchase\b", re.IGNORECASE),
    re.compile(r"\bpre[ _-]?order\b", re.IGNORECASE),
    re.compile(r"\bdigital\b", re.IGNORECASE),
    re.compile(r"\bstandard\b", re.IGNORECASE),
)


def normalize_name(name: object) -> str:
    """Canonical slug for a product name.

    1. Strip trademark / registered / copyright marks.
    2. Strip a trailing "(DLC)" suffix — Bandai's master catalog labels
       downloadable content with this tag; the royalty report does not.
    3. Strip the standalone word "Edition" — Bandai uses "(Deluxe Edition)";
       the royalty report uses bare "Deluxe".
    4. Strip "noise" tokens: "Pre-Purchase", "Pre-Order", "Digital",
       "Standard". These are sales-mechanic / formatting labels that
       don't identify a distinct SKU.
    5. Collapse any run of non-alphanumeric characters into "_", trim
       leading/trailing "_", lowercase.

    NaN / None / non-str → "".

    Examples:
        "ACE COMBAT 7: SKIES UNKNOWN"                  → "ace_combat_7_skies_unknown"
        "DARK SOULS™ III"                              → "dark_souls_iii"
        "Dark Souls III: Season Pass (DLC)"            → "dark_souls_iii_season_pass"
        "SCARLET NEXUS Deluxe Edition"                 → "scarlet_nexus_deluxe"
        "Super Robot Wars 30 Digital Deluxe Edition"   → "super_robot_wars_30_deluxe"
        "Little Nightmares III (pre-order)"            → "little_nightmares_iii"
        "Pre-Purchase Little Nightmares III Pre-Order" → "little_nightmares_iii"
    """
    if not isinstance(name, str):
        return ""
    s = name.replace("™", "").replace("®", "").replace("©", "")
    s = _DLC_SUFFIX_RE.sub("", s)
    s = _EDITION_WORD_RE.sub("", s)
    for noise_re in _NOISE_RES:
        s = noise_re.sub("", s)
    return _NON_ALNUM_RE.sub("_", s).strip("_").lower()
