"""
Preprocesses the raw Boston food-inspection CSV into a cleaned CSV with
normalized brand/location fields added, at the original row grain (one row
per violation, same as the source file).

Adds these columns to every row:
  - chain          : normalized brand name (e.g. "mcdonalds", "cvs").
                      Digits that are part of the brand itself ("Pho 2000",
                      "Cafe 1010") are preserved -- only a trailing store
                      number ("No. 2070", "#2070") or a parenthetical
                      address suffix ("(3060 Washington St.)") is stripped
                      before normalizing, since those encode a location,
                      not the brand.
  - norm_address    : lowercased, trimmed, whitespace-collapsed, suffix-
                      canonicalized address -- the join key for the 311
                      dataset's addr_norm column (see datasets_311/merge_311.py).
                      Computed by the shared addr_normalize.normalize_address,
                      so the two datasets can't drift out of sync again.
  - location_key    : identifies ONE physical establishment for a chain.
                      Normally this is the normalized street address (store
                      numbers in the name are inconsistent data-entry noise,
                      not a second physical dimension -- using them directly
                      over-splits a single spot's license renewals into fake
                      separate "locations").
                      EXCEPTION: when multiple DIFFERENTLY-NAMED variants of
                      the same chain (e.g. "Moyzilla", "Moyzilla No. 2",
                      "Moyzilla No. 3") have CONCURRENTLY active licenses at
                      the same address, that address really does host
                      multiple simultaneous stalls (e.g. Logan Airport food
                      court) -- in that case location_key includes the raw
                      business name so each stall stays distinct.
  - zip_clean       : zip normalized to 5 digits (restores stripped leading
                      zeros, truncates ZIP+4 suffixes, blanks out "00000").

Rows are dropped (not just left unmatched) if their address is Logan Airport
or South Station -- see addr_normalize.SOUTH_STATION_ADDRESSES and
is_excluded_location for why those two can't be handled by a simple
substring/prefix rule on both datasets.

Usage:
    python preprocess.py [input_csv] [output_csv]
Defaults to tmps7_x9w7x.csv -> tmps7_x9w7x_clean.csv in the same folder.
"""

import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from addr_normalize import is_excluded_location, normalize_address

DEFAULT_IN = r"c:\Users\rgrom\Desktop\projects\toa-hackathon\tmps7_x9w7x.csv"
DEFAULT_OUT = r"c:\Users\rgrom\Desktop\projects\toa-hackathon\tmps7_x9w7x_clean.csv"

# Trailing "No. 2070" / "#2070" store-number suffix (must be at the END of
# the name so brand names containing numbers, like "Pho 2000", are untouched).
STORE_NO_SUFFIX_RE = re.compile(r"\s*(?:no\.?|#)\s*0*(\d+)\s*$", re.IGNORECASE)

# Trailing parenthetical address, e.g. "McDonald's(3060 Washington St.)" or
# "Lenox Hotel (5 Food Serv. Loc.)" -- location detail bolted onto the name.
PAREN_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")


def strip_location_suffix(raw_name: str) -> str:
    """Remove a trailing store-number or parenthetical-address suffix from a
    RAW business name, so brand extraction only ever drops digits that are
    clearly a location marker, never digits that are part of the brand."""
    s = raw_name
    while True:
        new_s = PAREN_SUFFIX_RE.sub("", s)
        new_s = STORE_NO_SUFFIX_RE.sub("", new_s)
        if new_s == s:
            break
        s = new_s
    return s.strip()


def normalize_brand(name: str) -> str:
    """Lowercase, strip punctuation/apostrophes, collapse whitespace.
    Never removes digits -- brand names like 'Pho 2000' survive intact."""
    s = name.lower()
    s = re.sub(r"[\u2019'`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_zip(z) -> str:
    if pd.isna(z):
        return None
    s = str(z).strip()
    if not s or s == "00000":
        return None
    s = s.split("-")[0]   # drop ZIP+4 suffix
    s = s.zfill(5)          # restore stripped leading zeros
    if len(s) != 5 or not s.isdigit():
        return None
    return s


# A brief overlap between two licenses at the same address is normal
# renewal-transition noise (old license winding down while the new one is
# issued) -- e.g. a McDonald's re-licensed under a new name in Sep 2016
# while the old license didn't formally expire until Jan 2017. Only an
# overlap sustained longer than this counts as genuinely concurrent stalls.
MIN_CONCURRENT_OVERLAP = pd.Timedelta(days=180)


def date_range_overlaps(a_start, a_end, b_start, b_end) -> bool:
    if pd.isna(a_start) or pd.isna(a_end) or pd.isna(b_start) or pd.isna(b_end):
        return False
    overlap_start = max(a_start, b_start)
    overlap_end = min(a_end, b_end)
    return (overlap_end - overlap_start) >= MIN_CONCURRENT_OVERLAP


def resolve_multi_stall_addresses(df: pd.DataFrame) -> pd.Series:
    """Returns a location_key Series: normalized address by default, except
    for (chain, address) groups that host multiple differently-named,
    concurrently-active variants (genuine multi-stall food-court vendors),
    where the raw business name is folded into the key so each stall stays
    distinct instead of collapsing into one shared-address bucket."""
    location_key = df["norm_address"].copy()
    no_addr = location_key.isna()
    location_key[no_addr] = "license-" + df.loc[no_addr, "licenseno"].astype(str)

    for (chain, addr), idx in df.groupby(["chain", "norm_address"]).groups.items():
        if addr is None or len(idx) < 2:
            continue
        sub = df.loc[idx, ["businessname", "issdttm_parsed", "expdttm_parsed"]]
        sub = sub.drop_duplicates(subset=["businessname", "issdttm_parsed", "expdttm_parsed"])

        variants = sub["businessname"].unique()
        if len(variants) < 2:
            continue  # one name variant here -- normal renewal history, not multi-stall

        by_variant = {
            v: sub[sub["businessname"] == v][["issdttm_parsed", "expdttm_parsed"]].to_dict("records")
            for v in variants
        }
        concurrent_pair_found = False
        for i in range(len(variants)):
            for j in range(i + 1, len(variants)):
                for a in by_variant[variants[i]]:
                    for b in by_variant[variants[j]]:
                        if date_range_overlaps(a["issdttm_parsed"], a["expdttm_parsed"],
                                                b["issdttm_parsed"], b["expdttm_parsed"]):
                            concurrent_pair_found = True
                            break
                    if concurrent_pair_found:
                        break
                if concurrent_pair_found:
                    break
            if concurrent_pair_found:
                break

        if not concurrent_pair_found:
            continue

        location_key.loc[idx] = df.loc[idx, "norm_address"] + " :: " + df.loc[idx, "businessname"]

    return location_key


def main():
    in_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT_IN)
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(DEFAULT_OUT)

    df = pd.read_csv(in_path, low_memory=False, dtype={"zip": str})
    df = df.dropna(subset=["businessname"]).reset_index(drop=True)

    brand_raw = df["businessname"].map(strip_location_suffix)
    df["chain"] = brand_raw.map(normalize_brand)
    df["norm_address"] = df["address"].map(normalize_address)
    df["zip_clean"] = df["zip"].map(normalize_zip)

    excluded = df["norm_address"].map(is_excluded_location)
    n_excluded = int(excluded.sum())
    if n_excluded:
        print(f"dropping {n_excluded} rows at Logan Airport / South Station")
        df = df.loc[~excluded].reset_index(drop=True)

    df["issdttm_parsed"] = pd.to_datetime(df["issdttm"], errors="coerce", utc=True)
    df["expdttm_parsed"] = pd.to_datetime(df["expdttm"], errors="coerce", utc=True)

    df["location_key"] = resolve_multi_stall_addresses(df)

    df = df.drop(columns=["issdttm_parsed", "expdttm_parsed"])

    df.to_csv(out_path, index=False)

    print(f"input rows: {len(df)}")
    print(f"unique chains: {df['chain'].nunique()}")
    print(f"unique (chain, location_key) pairs: {df.groupby(['chain', 'location_key']).ngroups}")
    print(f"wrote: {out_path}")


if __name__ == "__main__":
    main()
