"""Typed DuckDB build over the Analyze Boston CSVs in data/downloads.

router.py's SCHEMA_CARD and GOLDEN queries assume typed tables named
requests/crime/inspections/inspection_violations/permits/assessment/
crashes/budget already exist in boston.db. This module builds them from the
raw CSVs (currency strings, comma-formatted numbers, optional tz-offset
timestamps and all) and connects with TimeZone=UTC so date arithmetic is
reproducible across laptops -- see router.py's `_connect()` docstring for
why that matters.

Run:  python -m boston.duck            # build/rebuild boston.db from the CSVs
      python -m boston.duck --force    # rebuild even if boston.db already has the tables
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = REPO_ROOT / "data" / "downloads"
DB_PATH = str(REPO_ROOT / "boston.db")

TABLES = ("requests", "crime", "inspections", "inspection_violations",
          "permits", "assessment", "crashes", "budget")


def _csv(name: str) -> str:
    path = DOWNLOADS / name
    if not path.exists():
        raise SystemExit(f"Missing {path} -- expected it under data/downloads.")
    return str(path)


def _money(col: str) -> str:
    # "$36,500.00" / " $10,203.96 " -> 36500.00
    return f"TRY_CAST(replace(replace(trim({col}), '$', ''), ',', '') AS DOUBLE)"


def _num(col: str) -> str:
    # "822,900" -> 822900
    return f"TRY_CAST(replace({col}, ',', '') AS DOUBLE)"


def _ts(col: str) -> str:
    # strips an optional trailing UTC offset ("+00") before casting
    return f"TRY_CAST(regexp_replace({col}, '\\+\\d\\d(:?\\d\\d)?$', '') AS TIMESTAMP)"


def _build_requests(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE OR REPLACE TABLE requests AS
        SELECT
            case_enquiry_id,
            {_ts('open_dt')} AS open_dt,
            {_ts('closed_dt')} AS closed_dt,
            (closed_dt IS NOT NULL) AS is_closed,
            date_diff('day', {_ts('open_dt')}, {_ts('closed_dt')}) AS days_to_close,
            year({_ts('open_dt')}) AS open_year,
            month({_ts('open_dt')}) AS open_month,
            on_time, case_status, closure_reason, case_title, subject, reason,
            type, department, neighborhood,
            location_zipcode AS zip5,
            TRY_CAST(latitude AS DOUBLE) AS latitude,
            TRY_CAST(longitude AS DOUBLE) AS longitude,
            source, ward
        FROM read_csv_auto('{_csv("311-service-requests.csv")}', ALL_VARCHAR=TRUE)
    """)


def _build_crime(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE OR REPLACE TABLE crime AS
        SELECT
            INCIDENT_NUMBER AS incident_number,
            TRY_CAST(OFFENSE_CODE AS INTEGER) AS offense_code,
            OFFENSE_DESCRIPTION AS offense_description,
            DISTRICT AS district,
            (SHOOTING = '1') AS shooting,
            {_ts('OCCURRED_ON_DATE')} AS occurred_on,
            TRY_CAST(YEAR AS INTEGER) AS year,
            TRY_CAST(MONTH AS INTEGER) AS month,
            trim(DAY_OF_WEEK) AS day_of_week,
            TRY_CAST(HOUR AS INTEGER) AS hour,
            STREET AS street,
            TRY_CAST(Lat AS DOUBLE) AS latitude,
            TRY_CAST(Long AS DOUBLE) AS longitude
        FROM read_csv_auto('{_csv("crime-incident-reports.csv")}', ALL_VARCHAR=TRUE)
    """)


def _build_inspections(con: duckdb.DuckDBPyConnection) -> None:
    # inspection_violations: the raw file, one row per violation cited.
    con.execute(f"""
        CREATE OR REPLACE TABLE inspection_violations AS
        SELECT
            businessname, licenseno,
            result,
            {_ts('resultdttm')} AS resultdttm,
            violation, viol_level, violdesc, comments,
            address, zip AS zip5, property_id
        FROM read_csv_auto('{_csv("food-establishment-inspections.csv")}', ALL_VARCHAR=TRUE)
    """)
    # inspections: collapsed to event grain (one row per actual inspection).
    # Same (licenseno, resultdttm) pair recurs once per violation cited at
    # that visit -- see router.py's SCHEMA_CARD, which flags the ~4x overcount
    # from treating inspection_violations rows as inspection counts.
    con.execute("""
        CREATE OR REPLACE TABLE inspections AS
        SELECT
            licenseno,
            resultdttm,
            any_value(businessname) AS businessname,
            any_value(address) AS address,
            any_value(zip5) AS zip5,
            any_value(result) AS result,
            year(resultdttm) AS result_year,
            count(*) AS n_violations,
            count(*) FILTER (WHERE viol_level = '***') AS n_critical
        FROM inspection_violations
        WHERE licenseno IS NOT NULL AND resultdttm IS NOT NULL
        GROUP BY licenseno, resultdttm
    """)
    # licensecat lives on the raw CSV, not carried through the GROUP BY above.
    con.execute(f"""
        ALTER TABLE inspections ADD COLUMN licensecat VARCHAR;
        UPDATE inspections i SET licensecat = (
            SELECT any_value(licensecat) FROM read_csv_auto(
                '{_csv("food-establishment-inspections.csv")}', ALL_VARCHAR=TRUE) r
            WHERE r.licenseno = i.licenseno AND {_ts('r.resultdttm')} = i.resultdttm
        )
    """)


def _build_permits(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE OR REPLACE TABLE permits AS
        SELECT
            permitnumber, worktype, description, comments, applicant,
            {_money('declared_valuation')} AS declared_valuation_usd,
            {_money('total_fees')} AS total_fees_usd,
            {_ts('issued_date')} AS issued_date,
            year({_ts('issued_date')}) AS issued_year,
            status, occupancytype,
            TRY_CAST(sq_feet AS DOUBLE) AS sq_feet,
            address, zip AS zip5, ward, property_id, parcel_id,
            TRY_CAST(y_latitude AS DOUBLE) AS latitude,
            TRY_CAST(x_longitude AS DOUBLE) AS longitude
        FROM read_csv_auto('{_csv("approved-building-permits.csv")}', ALL_VARCHAR=TRUE)
    """)


def _build_assessment(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE OR REPLACE TABLE assessment AS
        SELECT
            PID AS pid, GIS_ID AS gis_id, ST_NAME AS st_name, CITY AS city,
            ZIP_CODE AS zip5, LU AS lu, LU_DESC AS lu_desc, OWNER AS owner,
            {_num('LAND_VALUE')} AS land_value,
            {_num('BLDG_VALUE')} AS bldg_value,
            {_num('TOTAL_VALUE')} AS total_value,
            {_money('GROSS_TAX')} AS gross_tax,
            TRY_CAST(YR_BUILT AS INTEGER) AS yr_built,
            OVERALL_COND AS overall_cond
        FROM read_csv_auto('{_csv("property-assessment.csv")}', ALL_VARCHAR=TRUE)
    """)


def _build_crashes(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE OR REPLACE TABLE crashes AS
        SELECT
            {_ts('dispatch_ts')} AS dispatch_ts,
            year({_ts('dispatch_ts')}) AS year,
            month({_ts('dispatch_ts')}) AS month,
            mode_type, location_type, street, xstreet1, xstreet2,
            TRY_CAST(lat AS DOUBLE) AS latitude,
            TRY_CAST(long AS DOUBLE) AS longitude
        FROM read_csv_auto('{_csv("vision-zero-crash-records.csv")}', ALL_VARCHAR=TRUE)
    """)


def _build_budget(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE OR REPLACE TABLE budget AS
        SELECT
            Cabinet AS cabinet, Dept AS dept, Program AS program,
            "Expense Category" AS expense_category,
            TRY_CAST("FY24 Actual Expense" AS DOUBLE) AS fy24_actual,
            TRY_CAST("FY25 Actual Expense" AS DOUBLE) AS fy25_actual,
            TRY_CAST("FY26 Appropriation" AS DOUBLE) AS fy26_appropriation,
            TRY_CAST("FY27 Budget" AS DOUBLE) AS fy27_budget
        FROM read_csv_auto('{_csv("operating-budget.csv")}', ALL_VARCHAR=TRUE)
    """)


BUILDERS = {
    "requests": _build_requests,
    "crime": _build_crime,
    "inspections": _build_inspections,  # also builds inspection_violations
    "permits": _build_permits,
    "assessment": _build_assessment,
    "crashes": _build_crashes,
    "budget": _build_budget,
}


def build(con: duckdb.DuckDBPyConnection) -> None:
    for name, fn in BUILDERS.items():
        print(f"  building {name}…")
        fn(con)
    counts = con.execute(
        "SELECT 'requests', count(*) FROM requests UNION ALL "
        "SELECT 'crime', count(*) FROM crime UNION ALL "
        "SELECT 'inspections', count(*) FROM inspections UNION ALL "
        "SELECT 'inspection_violations', count(*) FROM inspection_violations UNION ALL "
        "SELECT 'permits', count(*) FROM permits UNION ALL "
        "SELECT 'assessment', count(*) FROM assessment UNION ALL "
        "SELECT 'crashes', count(*) FROM crashes UNION ALL "
        "SELECT 'budget', count(*) FROM budget"
    ).fetchall()
    for name, n in counts:
        print(f"  {name}: {n} rows")


def connect() -> duckdb.DuckDBPyConnection:
    """Open boston.db, building the typed tables from CSV on first use."""
    con = duckdb.connect(DB_PATH)
    con.execute("SET TimeZone='UTC'")
    existing = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if not set(TABLES).issubset(existing):
        build(con)
    return con


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="rebuild even if tables already exist")
    args = ap.parse_args()
    con = duckdb.connect(DB_PATH)
    con.execute("SET TimeZone='UTC'")
    existing = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if args.force or not set(TABLES).issubset(existing):
        build(con)
    else:
        print(f"{DB_PATH} already has all tables — pass --force to rebuild.")


if __name__ == "__main__":
    main()
