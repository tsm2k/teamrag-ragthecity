# Boston Property Assessment Data — Agent Reference

Reference doc for an orchestrator agent doing text-to-SQL over this dataset.
Read this before writing a query — several columns behave in non-obvious
ways that will silently produce wrong answers if you guess.

## 1. What this is

City of Boston property assessment records, one CSV per fiscal year on
Analyze Boston, merged here into a single wide table covering **FY2024,
FY2025, FY2026**. Each fiscal year is effectively a snapshot: "here is every
taxable/exempt parcel-building in the city and its assessed value as of this
assessment cycle." FY2023 is intentionally excluded (see §7).


- **Rows**: 185,126 — **Columns**: 75
- 181,658 rows (98%) have data in all 3 years; the rest have partial history
  because the property didn't exist yet, was newly subdivided, or dropped
  out (see `YEARS_PRESENT`, §5)

## 2. Grain — read this first

**One row = one building on one parcel for one fiscal-year-agnostic
identity**, keyed by `PID` + `BLDG_SEQ`, NOT one row per property. A parcel
with multiple buildings has multiple rows sharing the same `PID` with
different `BLDG_SEQ`. `PID` alone is **not unique** in this table.

Money/valuation columns are the only ones that vary by year and are
year-suffixed: `TOTAL_VALUE_2024`, `TOTAL_VALUE_2025`, `TOTAL_VALUE_2026`
(same pattern for `LAND_VALUE`, `BLDG_VALUE`, `GROSS_TAX`, `SFYI_VALUE`).
There is **no unsuffixed "current value" column** — for "what's this
property worth now," use the `_2026` columns (the latest year). Every other
column (address, owner, structural characteristics) holds a single value:
whatever that field was in the most recent fiscal year the row appears in.

## 3. Full column reference

Types below are what you should cast to when loading into a SQL engine —
several look numeric but must stay as text (leading zeros).

| Column | Type | Null% | Meaning |
|---|---|---|---|
| `PID` | TEXT (10 digits, zero-padded) | 0% | Parcel/unit ID. Primary identifier but not unique alone — pair with `BLDG_SEQ`. |
| `BLDG_SEQ` | TEXT/INT | 0% | Building index within the parcel (usually `1`). Part of the row key. |
| `CM_ID` | TEXT | 48% | **Condo Master ID** — for a condo unit or its master shell, the `PID` of that building's `CONDO MAIN` record. Null for non-condo properties. See §6. |
| `GIS_ID` | TEXT | 0% | Map/parcel identifier. Shared by every unit + the master record on the same physical parcel. |
| `ST_NUM` | TEXT | 5% | Street number. |
| `ST_NUM2` | TEXT | 88% | Secondary street number for address ranges ("10-**12**"). Only exists from FY2025 on. |
| `ST_NAME` | TEXT | 0% | Street name. |
| `UNIT_NUM` | TEXT | 54% | Condo/apartment unit number. |
| `CITY` | TEXT | 0% | Mostly official Boston neighborhoods (ALLSTON, DORCHESTER, ROXBURY, ...). Also contains BROOKLINE, NEWTON, DEDHAM, CHESTNUT HILL for a handful of border parcels — reflects postal/ZIP city naming, not a guarantee the parcel sits outside Boston's assessing jurisdiction. Don't assume `CITY != 'BOSTON'` means "exclude from Boston analysis." |
| `ZIP_CODE` | TEXT (5 digits, zero-padded) | 0% | Property ZIP. 38 distinct values. |
| `NUM_BLDGS` | INT | 0% | Number of buildings on the parcel. |
| `LUC` | TEXT (3 digits, zero-padded) | 0% | Numeric land-use code, e.g. `"105"`. Finer-grained than `LU`. |
| `LU` | TEXT | 0% | Land-use category code (`R1`, `R2`, `R3`, `R4`, `RC`, `RL`, `A`, `C`, `CC`, `CD`, `CL`, `CM`, `CP`, `E`, `EA`, `I`). See §7 for a data-quality quirk on this column. |
| `LU_DESC` | TEXT | 0% | Human-readable land use, e.g. `"THREE-FAM DWELLING"`, `"CONDO MAIN"`, `"SINGLE FAM DWELLING"`. Most reliable field for "what kind of property is this" filters. |
| `BLDG_TYPE` | TEXT | 2% | Building form, `"code - description"` e.g. `"RE - Row End"`, `"HR - High Rise"`. |
| `OWN_OCC` | TEXT (`Y`/`N`) | 0% | Owner-occupied flag. |
| `OWNER` | TEXT | 0% | Owner name as of latest year. |
| `MAIL_ADDRESSEE` | TEXT | 81% | Owner's mailing addressee (e.g. c/o name), when different from `OWNER`. |
| `MAIL_STREET_ADDRESS` | TEXT | 0% | Owner's mailing street address. |
| `MAIL_CITY` | TEXT | 0% | Owner's mailing city. |
| `MAIL_STATE` | TEXT | 0.2% | Owner's mailing state. |
| `MAIL_ZIP_CODE` | TEXT | 0% | Owner's mailing ZIP. |
| `RES_FLOOR` | INT | 18% | Residential floor number. |
| `CD_FLOOR` | INT | 60% | Condo unit's floor number. |
| `LAND_SF` | FLOAT | 5% | Land area, square feet. Range 100 – ~101,500,000 (large civic/institutional parcels). |
| `GROSS_AREA` | FLOAT | 19% | Gross building area, sq ft. |
| `LIVING_AREA` | FLOAT | 19% | Living area, sq ft. |
| `YR_BUILT` | INT (year) | 12% | Original construction year. See §7 for one known bad value. |
| `YR_REMODEL` | INT (year) | 52% | Last remodel year, when applicable. |
| `STRUCTURE_CLASS`, `RES_UNITS`, `COM_UNITS`, `RC_UNITS`, `KITCHEN_STYLE3`, `ST_ALPHA` | — | — | **Dropped** — over 90% null in this merge, see §7. |
| `ROOF_STRUCTURE`, `ROOF_COVER`, `INT_WALL`, `EXT_FNISHED`, `INT_COND`, `EXT_COND`, `OVERALL_COND` | TEXT | 5-27% | Condition/material ratings, `"code - description"` format (e.g. `"A - Average"`, `"F - Flat"`). |
| `BED_RMS`, `FULL_BTH`, `HLF_BTH`, `KITCHENS`, `TT_RMS` (total rooms), `FIREPLACES`, `NUM_PARKING` | INT | 7-27% | Room/feature counts. |
| `BDRM_COND`, `BTHRM_STYLE1/2/3`, `KITCHEN_TYPE`, `KITCHEN_STYLE1/2`, `HEAT_TYPE`, `HEAT_SYSTEM`, `AC_TYPE`, `ORIENTATION`, `PROP_VIEW`, `CORNER_UNIT` | TEXT | 26-83% | Structural/condition detail fields, `"code - description"` format. Sparse for commercial/land-only records where they don't apply. |
| `YEARS_PRESENT` | TEXT | 0% | Comma-separated years this key appears in, e.g. `"2024,2025,2026"` or `"2025,2026"`. See §5. |
| `REBUILD_DETECTED` | BOOLEAN (as text `True`/`False`) | 0% | See §5. |
| `LAND_USE_CHANGED` | BOOLEAN (as text) | 0% | See §5. |
| `IS_CONDO_MASTER` | BOOLEAN (as text) | 0% | See §5 and §6. |
| `TOTAL_VALUE_2024/2025/2026` | FLOAT | 0.3-1.6% | Total assessed value ($). Range $0 – ~$2.45B, median ~$671K (2026). |
| `LAND_VALUE_2024/2025/2026` | FLOAT | 0.3-1.6% | Assessed land value ($). |
| `BLDG_VALUE_2024/2025/2026` | FLOAT | 0.3-1.6% | Assessed building value ($). |
| `GROSS_TAX_2024/2025/2026` | FLOAT | 10-12% | Annual property tax bill ($). Sparser than value columns — some parcels are tax-exempt and simply have no bill. |
| `SFYI_VALUE_2024/2025/2026` | FLOAT | 0.3-1.6% | "Special features / yard items" value ($) — pools, extra structures, etc. Usually `0`. |

## 4. Boolean/coded field conventions

- Many descriptive fields are formatted `"<code> - <description>"` in one
  string (e.g. `"A - Average"`, `"W - Ht Water/Steam"`). Match on the whole
  string or use `LIKE '%Average%'`/`LIKE 'A -%'` — don't assume the code
  alone (`"A"`) is stored separately.
- `REBUILD_DETECTED`, `LAND_USE_CHANGED`, `IS_CONDO_MASTER` are Python
  booleans serialized as the literal text `True`/`False` in the CSV. Cast
  explicitly (`= 'True'` or convert to a real BOOLEAN column) — most SQL
  engines won't auto-coerce this from a TEXT column.

## 5. Derived columns — why they exist and how to use them

These three flags exist because the join key (`PID`+`BLDG_SEQ`) is a
per-parcel building *index*, not a stable building identity — it can
legitimately point at two different real-world things across years.

- **`REBUILD_DETECTED`** (259 rows): `YR_BUILT` changed across the years
  this key appears. Original construction year should never move for the
  same physical building, so a change means the old building was demolished
  and a new one built, reusing the same `PID`+`BLDG_SEQ`. **For these rows,
  don't compute a "value change 2024→2026" — the earlier and later years
  describe different buildings.**
- **`LAND_USE_CHANGED`** (1,926 rows): `LU_DESC` changed across the years
  this key appears, with no rebuild (e.g. `THREE-FAM DWELLING` →
  `CONDO MAIN`, `SINGLE FAM DWELLING` → `THREE-FAM DWELLING`). Same
  structure, but the legal/use classification changed — valuation trend
  comparisons across the change point are not apples-to-apples.
- **`IS_CONDO_MASTER`** (11,139 rows): latest `LU_DESC` is `"CONDO MAIN"`,
  the condo association's shell record. Its `TOTAL_VALUE` is at or near `$0`
  by design — the real dollar value lives on the individual unit rows (see
  §6). **A `$0` value with `IS_CONDO_MASTER = True` means "value lives
  elsewhere," not "worthless" or "demolished."** When summing property value
  across a neighborhood/city, either exclude `IS_CONDO_MASTER = True` rows
  or you'll undercount nothing (they're already ~$0) — but don't report
  their $0 as a meaningful figure on its own.

`YEARS_PRESENT` tells you which fiscal years actually have data for this
row — check it before assuming a NULL value column means bad data. A
property with `YEARS_PRESENT = "2025,2026"` and `TOTAL_VALUE_2024 = NULL`
simply didn't exist as this PID in FY2024 (new construction, or a newly
subdivided condo unit — see §7).

## 6. The condo relationship: GIS_ID, CM_ID, IS_CONDO_MASTER

When a building converts to condos, Boston Assessing creates:
- One `CONDO MAIN` record (the association shell) — `IS_CONDO_MASTER = True`,
  `TOTAL_VALUE ≈ $0`.
- One record per unit — `LU_DESC` like `"RESIDENTIAL CONDO"`, each with its
  own real `TOTAL_VALUE`.

All of these share the same `GIS_ID` (the underlying land parcel), and every
unit's `CM_ID` equals the master record's `PID`. Example (217 Lexington ST):

| PID | GIS_ID | CM_ID | LU_DESC | UNIT_NUM | IS_CONDO_MASTER |
|---|---|---|---|---|---|
| 0100010000 | 0100010000 | 0100010000 | CONDO MAIN | — | True |
| 0100010002 | 0100010000 | 0100010000 | RESIDENTIAL CONDO | 1 | False |
| 0100010004 | 0100010000 | 0100010000 | RESIDENTIAL CONDO | 2 | False |
| 0100010006 | 0100010000 | 0100010000 | RESIDENTIAL CONDO | 3 | False |

**To reconstruct a whole building's value** (e.g. "what's this condo
building worth in total"), `GROUP BY CM_ID` (or `GIS_ID`) and
`SUM(TOTAL_VALUE_20XX)` across the unit rows — exclude the
`IS_CONDO_MASTER = True` row from the sum (it's ~$0, but excluding it keeps
the query's intent explicit). **To find all units in a building** given one
unit's `PID`, look up its `CM_ID`, then `WHERE CM_ID = <that value>`.

## 7. Known data quality gotchas

- **FY2023 is excluded on purpose.** It ships owner mail address as one
  combined field and `HEAT_FUEL` instead of FY2024+'s split `MAIL_*` fields
  and `HEAT_SYSTEM` — incompatible schemas, so merging it in would have
  meant losing the mail-address split. If FY2023 data is ever needed, it's
  a separate file (`data/propert_data/fy2023-property-assessment-data.csv`)
  not covered by this reference.
- **`LU` has two spellings for the same category**: `"RL"` (1 row) and
  `"RL - RL"` (5,923 rows) both mean "residential land." A filter on
  `LU = 'RL'` alone will miss almost all of them — use
  `LU LIKE 'RL%'` or `LU IN ('RL', 'RL - RL')`. `LU_DESC` is generally
  more reliable for filtering by property type.
- **One bad `YR_BUILT` value**: PID `1701545008` has `YR_BUILT = 20198`
  (clearly a data-entry typo in the source). Bound year queries, e.g.
  `WHERE YR_BUILT BETWEEN 1700 AND 2026`, rather than trusting `MAX(YR_BUILT)`.
- **Columns dropped for being >90% null**: `RES_UNITS`, `COM_UNITS`,
  `RC_UNITS`, `STRUCTURE_CLASS`, `KITCHEN_STYLE3`, `ST_ALPHA`. They exist in
  the raw yearly files but not in this merged table.
- **Parcel subdivision has no historical link.** When a building converts
  to condos, the original parcel PID can retire entirely and get replaced
  by many brand-new per-unit PIDs — one real example (430 Stuart St) went
  from 2 PIDs to 133 in a single year. Those new unit PIDs correctly show no
  data before the year they first appear (`YEARS_PRESENT` starts late) —
  this is not missing data, and there's no reliable way to back-compute a
  fair prior per-unit value from the old parent record's total.
- **`GROSS_TAX` and money fields were originally comma/currency-formatted
  text** (e.g. `" $8,632.80 "`, `"101,513,565"` for `LAND_SF`) in the raw
  source files. This merge already cleans and stores them as plain numbers
  — if you ever regenerate against a new raw file with a similar format
  quirk, check `boston/merge_property_data.py`'s `VALUE_COLS` /
  `STATIC_NUMERIC_COLS` cleaning step still covers it.
- **Fiscal year convention**: Massachusetts municipal assessments are
  typically valued as of January 1 of the prior calendar year and billed
  over a July–June fiscal year (e.g. an FY2026 bill generally reflects a
  Jan 1, 2025 valuation date) — treat "FY2026 value" as "the value on file
  as of that assessment cycle," not literally "value during calendar 2026."

## 8. Query tips

- **"Current value"** → use the `_2026` columns (most recent year), unless
  the user specifies a year.
- **"How has value changed"** → compute `TOTAL_VALUE_2026 - TOTAL_VALUE_2024`
  (or the relevant year pair), and check `REBUILD_DETECTED` /
  `LAND_USE_CHANGED` first — if either is `True`, qualify the answer
  ("this property was rebuilt/converted during this period, so the
  earlier and later values aren't directly comparable").
- **"Total value of a neighborhood/city"** → `SUM(TOTAL_VALUE_2026) WHERE
  CITY = ...`; consider whether to exclude `IS_CONDO_MASTER = True` rows
  depending on whether the question wants "official parcel count" or
  "actual owned value" (the master rows contribute ~$0 either way, but
  including them can double-count NUM_BLDGS-style logic).
- **"How many properties/buildings"** → be explicit about whether that
  means distinct `PID` (parcels) or distinct `PID`+`BLDG_SEQ` (buildings) —
  they differ for multi-building parcels.
- **Address search** → match on `ST_NUM` + `ST_NAME` (+ `ST_NUM2` for range
  addresses since FY2025, `UNIT_NUM` for condo units). `ST_NAME` casing
  is inconsistent (e.g. `"CHARLES ST"` vs `"Lexington ST"`) — use
  case-insensitive comparison (`UPPER(ST_NAME) = UPPER(...)`).
- **Never treat NULL value columns as `$0`** in aggregates — a NULL means
  "didn't exist that year" (check `YEARS_PRESENT`), a `0` is a real
  assessed value (common for exempt/condo-master/land-only records).

## 9. Quick facts

- 185,126 rows, 75 columns, covering FY2024–FY2026.
- 181,658 rows (98%) present in all 3 years.
- 19 distinct `CITY` values (18 Boston neighborhoods + a few border-parcel
  exceptions), 38 distinct `ZIP_CODE` values.
- `OWN_OCC`: 78,069 owner-occupied (`Y`), 107,057 not (`N`).
- 259 rebuild cases, 1,926 land-use-change cases, 11,139 condo-master shells.
- `TOTAL_VALUE_2026` ranges $0 – ~$2.45B, median ~$671,000.
