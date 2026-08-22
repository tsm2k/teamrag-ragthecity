"""Merge the FY2024-2026 property-assessment CSVs into one wide table.

FY2023 is excluded: it ships a single combined OWNER MAIL ADDRESS field and
HEAT_FUEL instead of FY2024+'s split MAIL_* fields and HEAT_SYSTEM, so mixing
it in means losing the mail-address split. FY2024/2025/2026 share an
identical 65-column schema (FY2025 adds ST_NUM2, FY2026 adds ST_ALPHA — both
address refinements, harmless as static columns).

PID is the durable parcel key; BLDG_SEQ breaks ties on multi-building
parcels. Valuation fields genuinely change year to year, so those get
per-year columns (TOTAL_VALUE_2024 .. TOTAL_VALUE_2026); everything else
(address, land use, structural characteristics) is taken from the most
recent fiscal year a property appears in.

Caveats — three ways this key can silently span two different real-world
things, each flagged rather than fixed (fixing would mean inventing data):
  * REBUILD_DETECTED — BLDG_SEQ is a per-parcel building *index*, not a
    stable building identity. YR_BUILT should never change for the same
    building, so a change across the years a key appears means a
    torn-down-and-rebuilt property reused the same PID+BLDG_SEQ. Treat the
    earlier-year valuation and later-year characteristics as two different
    buildings, not one continuous history.
  * LAND_USE_CHANGED — LU_DESC changes across the years a key appears (e.g.
    THREE-FAM DWELLING -> CONDO MAIN) with no rebuild. Same structure, but a
    legal/use-type conversion means valuation isn't apples-to-apples across
    the change either.
  * IS_CONDO_MASTER — latest LU_DESC is the condo association's "CONDO MAIN"
    shell record, which reports TOTAL_VALUE near/at $0 once a building
    converts, because the real dollar value moved to the individual unit
    PIDs (each condo unit gets its own PID+BLDG_SEQ). A $0 value here means
    "value lives elsewhere," not "worthless" or "demolished."

Not flagged, and not fixable from this data alone: when a building converts
to condos, the parent PID/BLDG_SEQ can retire entirely and get replaced by
many brand-new per-unit PIDs (one real Boston parcel went from 2 PIDs to 133
in one year). Those new unit PIDs will show no history before the year they
first appear — that's not missing data, the units didn't separately exist
before, and there's no reliable way to backfill a fair per-unit prior value
from the old parent record's total.

Run:  python boston/merge_property_data.py
"""
from __future__ import annotations

from functools import reduce
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data" / "propert_data"
OUT_PATH = ROOT_DIR / "final_data" / "boston_property_assessments_fy2024_2026.csv"

FILES = {
    2024: "fy2024-property-assessment-data_1_5_2024.csv",
    2025: "fy2025-property-assessment-data_12_30_2024.csv",
    2026: "fy2026-property-assessment-data_rev.csv",
}

KEY = ["PID", "BLDG_SEQ"]

# Fields that actually change between assessments — these get one column
# per year. All five are present in every FY2024-2026 file.
VALUE_COLS = ["TOTAL_VALUE", "LAND_VALUE", "BLDG_VALUE", "GROSS_TAX", "SFYI_VALUE"]

# Static (non-year-suffixed) fields that are numeric but ship as
# comma-formatted text (e.g. "101,513,565") — cleaned the same way as
# VALUE_COLS so they land in the output as real numbers, not text a SQL
# engine can't cast.
STATIC_NUMERIC_COLS = ["LAND_SF"]

# Columns this sparse in the merged output get dropped entirely.
NULL_DROP_THRESHOLD = 0.9


def load_year(year: int, filename: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / filename, dtype=str, low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    df = df.drop_duplicates()  # a handful of exact-duplicate rows per year
    df = df.drop_duplicates(subset=KEY, keep="first")  # a handful more share PID+BLDG_SEQ but differ elsewhere
    for col in VALUE_COLS + STATIC_NUMERIC_COLS:
        if col in df.columns:
            # GROSS_TAX ships as " $8,632.80 "; LAND_SF ships as "101,513,565" —
            # strip currency/thousands formatting before parsing, not just commas.
            cleaned = df[col].str.replace(r"[\$,]", "", regex=True).str.strip()
            df[col] = pd.to_numeric(cleaned, errors="coerce")
    df["_YEAR"] = year
    return df


def build_value_wide(years: dict[int, pd.DataFrame]) -> pd.DataFrame:
    frames = []
    for year, df in years.items():
        present = [c for c in VALUE_COLS if c in df.columns]
        renamed = df[KEY + present].rename(columns={c: f"{c}_{year}" for c in present})
        frames.append(renamed)
    return reduce(lambda left, right: pd.merge(left, right, on=KEY, how="outer"), frames)


def build_static_latest(years: dict[int, pd.DataFrame]) -> pd.DataFrame:
    static_cols_per_year = [df.drop(columns=VALUE_COLS, errors="ignore") for df in years.values()]
    combined = pd.concat(static_cols_per_year, ignore_index=True, sort=False)
    combined = combined.sort_values("_YEAR", ascending=False)
    latest = combined.drop_duplicates(subset=KEY, keep="first").drop(columns="_YEAR")
    return latest


def build_years_present(years: dict[int, pd.DataFrame]) -> pd.DataFrame:
    frames = [df[KEY].assign(_YEAR=year) for year, df in years.items()]
    combined = pd.concat(frames, ignore_index=True)
    grouped = (
        combined.groupby(KEY)["_YEAR"]
        .apply(lambda s: ",".join(str(y) for y in sorted(s)))
        .reset_index()
        .rename(columns={"_YEAR": "YEARS_PRESENT"})
    )
    return grouped


def build_changed_flag(years: dict[int, pd.DataFrame], column: str, flag_name: str) -> pd.DataFrame:
    """Flag keys where `column` takes more than one distinct value across the years they appear."""
    by_year = pd.DataFrame({year: df.set_index(KEY)[column] for year, df in years.items()})
    changed = by_year.apply(lambda r: r.dropna().nunique() > 1, axis=1)
    return changed.rename(flag_name).reset_index()


def build_condo_master_flag(static: pd.DataFrame) -> pd.DataFrame:
    """Flag properties whose latest LU_DESC is the condo association's shell record.

    TOTAL_VALUE on a CONDO MAIN record reports near/at $0 because the real
    dollar value moved to the individual unit PIDs, not because the property
    is worthless.
    """
    is_master = static["LU_DESC"].str.strip().str.upper().eq("CONDO MAIN")
    return static[KEY].assign(IS_CONDO_MASTER=is_master)


def drop_sparse_columns(df: pd.DataFrame, protect: list[str]) -> pd.DataFrame:
    null_frac = df.isna().mean()
    to_drop = [c for c in df.columns if c not in protect and null_frac[c] > NULL_DROP_THRESHOLD]
    if to_drop:
        print(f"Dropping columns >{NULL_DROP_THRESHOLD:.0%} null: {to_drop}")
    return df.drop(columns=to_drop)


def main() -> None:
    years = {year: load_year(year, filename) for year, filename in FILES.items()}

    static = build_static_latest(years)
    values = build_value_wide(years)
    years_present = build_years_present(years)
    rebuild_flag = build_changed_flag(years, "YR_BUILT", "REBUILD_DETECTED")
    land_use_flag = build_changed_flag(years, "LU_DESC", "LAND_USE_CHANGED")
    condo_master_flag = build_condo_master_flag(static)

    merged = (
        static.merge(values, on=KEY, how="outer")
        .merge(years_present, on=KEY, how="left")
        .merge(rebuild_flag, on=KEY, how="left")
        .merge(land_use_flag, on=KEY, how="left")
        .merge(condo_master_flag, on=KEY, how="left")
    )

    value_year_cols = [f"{col}_{year}" for col in VALUE_COLS for year in FILES if f"{col}_{year}" in merged.columns]
    meta_cols = ["YEARS_PRESENT", "REBUILD_DETECTED", "LAND_USE_CHANGED", "IS_CONDO_MASTER"]
    other_cols = [c for c in merged.columns if c not in KEY + value_year_cols + meta_cols]
    merged = merged[KEY + other_cols + meta_cols + value_year_cols]

    merged = drop_sparse_columns(merged, protect=KEY + meta_cols + value_year_cols)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(OUT_PATH, index=False)

    print(f"Merged {len(merged):,} properties from {len(FILES)} fiscal years -> {OUT_PATH}")
    print(f"Columns: {len(merged.columns)}")
    print(f"Present in all {len(FILES)} years: {(merged['YEARS_PRESENT'].str.count(',') == len(FILES) - 1).sum():,}")
    print(f"Rebuild detected (YR_BUILT changed mid-series): {merged['REBUILD_DETECTED'].sum():,}")
    print(f"Land use changed mid-series: {merged['LAND_USE_CHANGED'].sum():,}")
    print(f"Condo master (association shell) records: {merged['IS_CONDO_MASTER'].sum():,}")


if __name__ == "__main__":
    main()
