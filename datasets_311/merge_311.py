"""
Merges the four Boston 311 CSV shards (one per calendar year: 2023, 2024,
2025, 2026-YTD) into a single deduplicated table.

The four files share an identical schema and near-zero overlap in
case_enquiry_id (verified: 3 duplicate ids out of ~895k rows total, all at
a year boundary) -- they are date-range shards of one export, not distinct
datasets, so they're concatenated and deduped rather than kept separate or
joined on anything.

Adds one column:
  - addr_norm : lowercased, trimmed, whitespace-collapsed, suffix-canonicalized
                location_street_name, for address-string joins against the
                food-safety dataset's own norm_address field (see
                food_inspections/preprocess.py). Computed by the shared
                addr_normalize.normalize_address so the two datasets can't
                drift out of sync again -- they previously abbreviated
                street suffixes differently (e.g. "Ave" here vs "AV" in the
                food-inspections raw data), which silently broke the join
                for every Avenue address until both sides were pointed at
                the same suffix table.

Rows are dropped (not just left unmatched) if their address is Logan Airport
or South Station -- see addr_normalize.SOUTH_STATION_ADDRESSES and
is_excluded_location. Note "logan" alone is NOT a safe filter here: South
Boston's "Logan Way" and Roxbury's "Logan St" are real, unrelated streets
that a bare substring match would incorrectly drop.

Usage:
    python merge_311.py [output_csv]
Defaults to 311_merged.csv in the same folder.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from addr_normalize import is_excluded_location, normalize_address

IN_DIR = Path(r"c:\Users\rgrom\Desktop\projects\toa-hackathon\datasets_311")
DEFAULT_OUT = IN_DIR / "311_merged.csv"

INPUT_FILES = [
    "tmp4myyj_u8.csv",
    "tmpik5_zdq4.csv",
    "tmpm461rr5o.csv",
    "tmpwbgyud93.csv",
]


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT

    frames = []
    for fname in INPUT_FILES:
        path = IN_DIR / fname
        df = pd.read_csv(path, low_memory=False)
        frames.append(df)
        print(f"read {fname}: {len(df)} rows")

    merged = pd.concat(frames, ignore_index=True)
    before = len(merged)
    merged = merged.drop_duplicates(subset=["case_enquiry_id"], keep="first")
    after = len(merged)
    print(f"concatenated: {before} rows, {before - after} duplicate case_enquiry_id dropped -> {after} rows")

    # format="mixed" is required: the 2026-YTD shard mixes timestamps with
    # no fractional seconds and 1-3 digit fractional seconds in the same
    # column, which breaks pandas' single-format inference and silently
    # coerces ~77% of that shard's open_dt to NaT otherwise.
    for col in ("open_dt", "closed_dt", "sla_target_dt"):
        parsed = pd.to_datetime(merged[col], format="mixed", errors="coerce")
        n_bad = parsed.isna().sum() - merged[col].isna().sum()
        if n_bad > 0:
            print(f"WARNING: {n_bad} values in {col} failed to parse and were coerced to NaT")
        merged[col] = parsed

    merged["addr_norm"] = merged["location_street_name"].map(normalize_address)

    excluded = merged["addr_norm"].map(is_excluded_location)
    n_excluded = int(excluded.sum())
    if n_excluded:
        print(f"dropping {n_excluded} rows at Logan Airport / South Station")
        merged = merged.loc[~excluded].reset_index(drop=True)

    merged = merged.sort_values("open_dt").reset_index(drop=True)

    merged.to_csv(out_path, index=False)

    print(f"unique case_enquiry_id: {merged['case_enquiry_id'].nunique()}")
    print(f"date range: {merged['open_dt'].min()} - {merged['open_dt'].max()}")
    print(f"wrote: {out_path}")


if __name__ == "__main__":
    main()
