"""
Shared address-normalization logic for joining the food-inspections dataset
(food_inspections/preprocess.py) against the 311 dataset (datasets_311/merge_311.py).

Both scripts import normalize_address from here instead of keeping their own
copy -- the two datasets abbreviate street suffixes differently (food-inspections
raw data: "AV", "BL", "HW", "PZ", "PW", "WY"; 311 raw data: "Ave", "Blvd", "Hwy",
"Plz", "Pkwy", "Way"), and normalizing each side independently let those
abbreviations silently diverge, breaking the join for entire street types
(worst case: every "Avenue" address, ~236k food-inspection rows).

SUFFIX_MAP canonicalizes both conventions onto the 311 (fuller) spelling, since
311 is the larger and more standard-looking source. Only the abbreviation is
touched -- SUFFIX_MAP keys are compared against the whole last whitespace-
delimited token of an already-lowercased address, so partial-word collisions
(e.g. "Charles Street South" -> ["street", "south"], last token "south") are
not affected by an unrelated key like "st".
"""

import re

# food-inspections abbreviation -> canonical 311-style spelling.
# 311's own abbreviation is included as a no-op mapping so both scripts can
# run every address through the same table.
SUFFIX_MAP = {
    "av": "ave", "ave": "ave",
    "bl": "blvd", "blvd": "blvd",
    "hw": "hwy", "hwy": "hwy",
    "pz": "plz", "plz": "plz",
    "pw": "pkwy", "pkwy": "pkwy",
    "wy": "way", "way": "way",
}


def normalize_address(a) -> str:
    """Lowercase, trim, collapse whitespace, canonicalize the trailing
    street-suffix abbreviation. Returns None for missing/blank input."""
    if not isinstance(a, str) or not a.strip():
        return None
    s = re.sub(r"\s+", " ", a.strip().lower())
    tokens = s.split(" ")
    last = tokens[-1]
    if last in SUFFIX_MAP:
        tokens[-1] = SUFFIX_MAP[last]
        s = " ".join(tokens)
    return s


# Addresses to exclude from BOTH datasets: Logan International Airport
# (East Boston) and South Station (Boston/Downtown). Both are large
# multi-tenant transit hubs where "address" identifies the terminal/building,
# not a single establishment's street location -- they don't behave like
# normal street addresses for either dataset's join purposes, and Logan in
# particular collides with real, unrelated street names ("Logan Way" in
# South Boston, "Logan St" in Roxbury) that must NOT be swept up by a naive
# substring match on "logan".
#
# Logan Airport: food-inspections addresses all normalize to a street number
# followed by "logan airport ..." (e.g. "200 logan airport trmnl b") -- the
# street number comes FIRST, so this is a substring check for the phrase
# "logan airport", not a prefix check. That phrase is what "Logan Way" and
# "Logan St" both lack, so they aren't caught by it. 311 has no rows
# addressed inside the airport itself, but the check is kept so this stays
# correct if that ever changes.
#
# South Station: not identifiable by a name/prefix rule -- the terminal's
# tenants are addressed as either the literal transit-hub address
# ("1 south station") or one of the surrounding building's street numbers
# on Atlantic Ave (630/640/680/700), confirmed present on both sides via
# the food-inspections businesses actually located there ("Cosi South
# Station" @ 630 Atlantic Ave, etc.) and matching 311 rows at the same
# numbers. Listed explicitly since there's no shared prefix to key off.
SOUTH_STATION_ADDRESSES = {
    "1 south station",
    "630 atlantic ave",
    "640 atlantic ave",
    "680 atlantic ave",
    "700 atlantic ave",
}


def is_excluded_location(norm_addr) -> bool:
    """True if a normalized address (post normalize_address) belongs to
    Logan Airport or South Station and should be dropped from both datasets."""
    if not isinstance(norm_addr, str):
        return False
    if "logan airport" in norm_addr:
        return True
    if norm_addr in SOUTH_STATION_ADDRESSES:
        return True
    return False
