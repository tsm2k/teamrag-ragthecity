"""
Clean data/tmp7uhweub2.csv (Boston food establishment inspection violations)
before loading it into pgvector via embed_openrouter.

Grain: one row = one violation citation within one inspection visit
(license + resultdttm + violation code). Multiple rows can share the
same inspection. `comments` is the free-text inspector note meant for
the embedding vector; every other column is metadata.

Run:  python boston/clean_food_inspections.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "data" / "tmp7uhweub2.csv"
DEST = REPO_ROOT / "data" / "food_inspections_clean.csv"

# result values collapse two generations of the source system's coding
# (pre-2010ish plain Pass/Fail, later HE_-prefixed codes) onto one axis.
RESULT_CATEGORY = {
    "HE_Pass": "pass", "Pass": "pass", "PassViol": "pass", "NoViol": "pass",
    "HE_Fail": "fail", "Fail": "fail", "Failed": "fail", "HE_FailExt": "fail", "HE_FAILNOR": "fail",
    "HE_Filed": "pending", "HE_Hearing": "pending", "HE_Hold": "pending", "HE_TSOP": "pending",
    "HE_NotReq": "not_required",
    "HE_VolClos": "closed", "HE_OutBus": "closed", "HE_Closure": "closed",
    "DATAERR": None, "HE_Misc": None,
}


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # --- text/whitespace normalization ---
    strip_cols = ["businessname", "dbaname", "legalowner", "namelast", "namefirst",
                  "address", "city", "comments", "violdesc"]
    for c in strip_cols:
        df[c] = df[c].str.strip().replace({"": pd.NA})
    df["address"] = df["address"].str.replace(r"\s+", " ", regex=True)

    # --- state: collapse MA / Ma / ma ---
    df["state"] = df["state"].str.strip().str.upper()

    # --- city: strip trailing slash artifacts and stray double-neighborhood combos ---
    df["city"] = (
        df["city"].str.strip().str.rstrip("/").str.replace(r"/+", "/", regex=True).replace({"": pd.NA})
    )

    # --- zip: zero-pad 4-digit zips (leading zero dropped somewhere upstream),
    # split ZIP+4 into base zip5 + zip4 extension, blank out the "00000" sentinel ---
    zip_raw = df["zip"].str.strip()
    zip5, zip4 = zip_raw.str.split("-", n=1, expand=False).str[0], zip_raw.str.split("-", n=1, expand=True)[1] if zip_raw.str.contains("-").any() else pd.Series([pd.NA] * len(df))
    zip5 = zip5.str.zfill(5)
    zip5 = zip5.mask(zip5 == "00000", pd.NA)
    df["zip"] = zip5
    df["zip4"] = zip4

    # --- property_id: "0" is a not-matched sentinel, not a real id ---
    df["property_id"] = df["property_id"].mask(df["property_id"] == "0", pd.NA)

    # --- viol_level: keep the real severity marks; blank out the two junk rows
    # (one is a literal "Test" record, one is stray whitespace) ---
    df["viol_level"] = df["viol_level"].where(df["viol_level"].isin(["*", "**", "***", "-"]), pd.NA)

    # --- viol_status: NaN and "" both mean "no violation status" (rows with no violation) ---
    df["viol_status"] = df["viol_status"].str.strip().replace({"": pd.NA})

    # --- result: raw value kept as-is; add a normalized 3-way category for filtering ---
    df["result_category"] = df["result"].map(RESULT_CATEGORY)

    # --- lat/lon pulled out of "(lat, lon)" for metadata filtering / geo queries ---
    coords = df["location"].str.extract(r"\(([-\d.]+),\s*([-\d.]+)\)")
    df["lat"] = pd.to_numeric(coords[0], errors="coerce")
    df["lon"] = pd.to_numeric(coords[1], errors="coerce")

    # --- parse all five datetime columns to real UTC timestamps ---
    for c in ["issdttm", "expdttm", "resultdttm", "violdttm", "status_date"]:
        df[c] = pd.to_datetime(df[c], errors="coerce", utc=True)

    # --- vector text: comments is the primary field, but ~10% are null
    # (mostly Pass rows with no violation to describe) — fall back to violdesc
    # so no row embeds an empty string ---
    df["vector_text"] = df["comments"].fillna(df["violdesc"])

    return df


def main() -> None:
    df = pd.read_csv(SRC, dtype=str, low_memory=False)
    cleaned = clean(df)
    cleaned.to_csv(DEST, index=False)
    print(f"Wrote {len(cleaned):,} rows to {DEST}")
    print(f"vector_text still null: {cleaned['vector_text'].isna().sum()}")


if __name__ == "__main__":
    main()
