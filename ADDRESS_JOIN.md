# Joining the 311 and food-inspections datasets

Two independent pipelines each add a normalized-address column so the two
datasets can be joined on a common street address:

- `food_inspections/preprocess.py` → `food_inspections/tmps7_x9w7x_clean.csv`
- `datasets_311/merge_311.py` → `datasets_311/311_merged.csv`

Both pipelines now import their address-normalization logic from a single
shared module, **`addr_normalize.py`** (repo root), instead of keeping their
own copy. That module is the source of truth for how an address string
becomes a join key, and for which addresses get dropped from both datasets
entirely.

## Why a shared module

The two source CSVs come from different city systems and abbreviate street
suffixes differently:

| Suffix | food-inspections raw (`address`) | 311 raw (`location_street_name`) |
|---|---|---|
| Avenue | `AV` | `Ave` |
| Boulevard | `BL` | `Blvd` |
| Highway | `HW` | `Hwy` |
| Plaza | `PZ` | `Plz` |
| Parkway | `PW` | `Pkwy` |
| Way | `WY` | `Way` |

Earlier versions of these two scripts each wrote their own normalization
function. They agreed on the easy part (lowercase, trim, collapse
whitespace) but neither one canonicalized the suffix abbreviation above, so
`"1033 commonwealth av"` (food-inspections) and `"1033 commonwealth ave"`
(311) normalized to two different strings and never joined — silently,
for every single Avenue/Boulevard/Highway/Plaza/Parkway/Way address in the
city (worst case ~236k food-inspection rows on Avenue alone). There was no
error; the join just quietly returned fewer matches than it should have.

Putting the logic in one importable module makes that class of bug
structurally impossible: both scripts call the exact same function, so they
cannot drift apart again as long as they keep importing it instead of
reimplementing it locally.

## `addr_normalize.py`

### `normalize_address(a) -> str | None`

Given a raw address string, returns:

1. `None` if the input isn't a non-blank string.
2. Otherwise: lowercase, trim leading/trailing whitespace, collapse any run
   of internal whitespace to a single space, then canonicalize the trailing
   suffix token using `SUFFIX_MAP` (e.g. the last token `av` or `ave` both
   become `ave`).

Only the **last whitespace-delimited token** of the address is checked
against `SUFFIX_MAP` — this is deliberate, so a word like `"south"` at the
end of `"1 charles street south"` isn't mistaken for a street-type
abbreviation, and so digits/unit letters glued onto a street number
(`"554a"`, `"20r"`) are left untouched.

### `SUFFIX_MAP`

A dict mapping every abbreviation seen in either source dataset to one
canonical spelling (currently the 311-style fuller abbreviation, since 311
is the larger of the two datasets). Both the food-inspections short form and
the 311 form map to the same output, so the table is safe to run every
address in both datasets through unconditionally.

### `is_excluded_location(norm_addr) -> bool`

Returns `True` for addresses that should be dropped from **both** datasets
before they're written out — currently Logan International Airport and
South Station. See "Excluded locations" below for why these two needed
special handling instead of a simple keyword filter.

## Excluded locations: Logan Airport and South Station

Both scripts drop rows at these two locations (not just leave them
unmatched) — the print output reports how many rows were removed
(`dropping N rows at Logan Airport / South Station`).

**Why exclude them at all:** both are large multi-tenant transit hubs where
the recorded "address" identifies the terminal or building, not a single
establishment's street location, so they don't behave like a normal street
address for join purposes (a food court with a dozen concurrent vendors
sharing one nominal address, or a train station's retail concourse).

**Why not a simple `"logan"` substring match:** South Boston has a real
street called **Logan Way**, and Roxbury has a real street called **Logan
St** — both named after a person, unrelated to the airport. A bare
substring filter on `"logan"` would incorrectly strip legitimate 311 rows
from those neighborhoods. Instead, exclusion is scoped to the phrase
`"logan airport"` appearing anywhere in the normalized address — every
Logan Airport row in the food-inspections data contains that exact phrase
(e.g. `"200 logan airport trmnl b"` — note the street number comes
*first*, so this must be a substring check, not a prefix check: an earlier
version of this filter used `.startswith("logan airport")`, which never
matched anything and silently let ~6,000 Logan Airport rows leak through
uncaught. Caught during verification and fixed to `"logan airport" in
norm_addr`). Neither "Logan Way" nor "Logan St" contains the word
"airport", so they're unaffected. 311 currently has zero rows addressed
inside the airport itself, so the check is a no-op on that side today, but
it's kept so this stays correct if that ever changes.

**Why South Station needed an explicit address list instead of a prefix
rule:** there's no shared "South Station" prefix in the address data on
either side. South Station's tenants are recorded under the surrounding
building's street address — `1 South Station`, or one of the Atlantic
Avenue building numbers (`630`, `640`, `680`, `700`). These were confirmed
by cross-checking actual businesses known to be inside South Station in the
food-inspections data (e.g. *Cosi South Station* → `630 Atlantic Ave`,
*Clarke's at South Station* → `640 Atlantic Ave`) against 311 rows at the
same street numbers. `SOUTH_STATION_ADDRESSES` in `addr_normalize.py` is
this explicit set:

```
1 south station
630 atlantic ave
640 atlantic ave
680 atlantic ave
700 atlantic ave
```

## New/changed columns

### `food_inspections/tmps7_x9w7x_clean.csv`

| Column | Type | Description |
|---|---|---|
| `chain` | str | Normalized brand name (unchanged by this update). |
| `norm_address` | str \| null | Normalized `address`, computed by `addr_normalize.normalize_address`. This is the join key against 311's `addr_norm`. Null if the source `address` was blank (e.g. Logan Airport rows before the exclusion filter runs, or malformed input). |
| `location_key` | str | Identifies one physical establishment for a chain (unchanged by this update — still built from `norm_address`, so it inherits the suffix-canonicalization fix automatically). |
| `zip_clean` | str \| null | 5-digit zip (unchanged by this update). |

**Behavior change:** rows whose `norm_address` is Logan Airport or South
Station are now dropped entirely from the output CSV, after `norm_address`
is computed but before `location_key` is derived. The script prints how many
rows were removed — as of the last run, **8,222 rows** (896,379 input rows
→ 888,157 written).

### `datasets_311/311_merged.csv`

| Column | Type | Description |
|---|---|---|
| `addr_norm` | str \| null | Normalized `location_street_name`, computed by the same `addr_normalize.normalize_address`. Null for ~0.9% of rows — mostly 311 requests with no street address attached (e.g. phone-only reports), not a bug. |

**Behavior change:** rows whose `addr_norm` is Logan Airport or South
Station are dropped entirely from the output CSV, after `addr_norm` is
computed and before the final sort/write. The script prints how many rows
were removed — as of the last run, **191 rows** (895,333 deduplicated rows
→ 895,142 written). 311 currently has no rows inside the airport itself, so
in practice only South Station rows are removed on this side.

## Verified join results (as of the last regeneration)

| | food_inspections | 311 |
|---|---|---|
| Rows written | 888,157 | 895,142 |
| Rows dropped (Logan/South Station) | 8,222 | 191 |
| Unique `norm_address`/`addr_norm` values | 4,605 | 117,567 |
| Null `norm_address`/`addr_norm` | 224 | 8,335 |

Joining on `norm_address == addr_norm`: **3,518 addresses** overlap between
the two datasets, matching **43,100 of 895,142** 311 rows (4.8%) to at least
one food-inspection address. This is roughly 3/4 of all `food_inspections`
addresses finding a match in 311 (3,518 / 4,605 non-null unique addresses),
consistent with 311 covering the whole city while food-inspections only
covers licensed food establishments.

For comparison, before the suffix-canonicalization fix (`av`/`ave` etc.)
the same join only found 2,469 overlapping addresses and 28,844 matching
311 rows — the fix recovered roughly 1,050 addresses and 14,000 previously
silently-dropped 311 rows, concentrated on Avenue/Boulevard/Highway/
Plaza/Parkway/Way addresses.

## How the two files interact

There is no automated join step in this repo yet — each script produces its
own CSV independently. To join them:

```python
import pandas as pd

food = pd.read_csv("food_inspections/tmps7_x9w7x_clean.csv", low_memory=False)
c311 = pd.read_csv("datasets_311/311_merged.csv", low_memory=False)

joined = food.merge(c311, left_on="norm_address", right_on="addr_norm", how="inner")
```

Notes for anyone doing this join:

- It's an **address-level** join, not an establishment-level join. A single
  street address can host multiple food-inspection rows (violation history)
  and multiple 311 rows (unrelated complaint types — street cleaning, trees,
  abandoned vehicles, as well as `Health`-reason complaints that are
  plausibly about the food establishment itself). Expect a many-to-many
  join; decide downstream whether you want all rows, only `reason == "Health"`
  311 rows, or some other filter.
- Not every food-inspection address will find a 311 match, and vice versa —
  311 covers all city addresses (fire hydrants, potholes, trees) while
  food-inspections only covers licensed food establishments, so the overlap
  is a small fraction of 311's address space but a majority-ish fraction of
  food-inspections' address space.
- If you regenerate either CSV, regenerate *both* — `location_key` in the
  food-inspections output is derived from `norm_address`, so any future
  change to `addr_normalize.normalize_address` changes both `norm_address`
  and `location_key` together, and 311's `addr_norm` needs to be
  regenerated in lockstep or the join key definitions will disagree again.

## Regenerating the outputs

```
python food_inspections/preprocess.py
python datasets_311/merge_311.py
```

Both scripts default to their standard input/output paths (see each
script's docstring) and can optionally take explicit input/output paths as
positional arguments. Close the output CSV in Excel or any other program
before rerunning — pandas' `to_csv` will fail with a `PermissionError` if
the file is open elsewhere.
