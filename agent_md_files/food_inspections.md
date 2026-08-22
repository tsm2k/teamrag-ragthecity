# Boston Food Inspection Data — Agent Reference

Reference doc for an orchestrator agent doing RAG (vector + metadata filter)
over this dataset. Read this before deciding what metadata to filter on or
how to interpret a retrieved chunk — several fields behave in non-obvious
ways.

## 1. What this is

Boston ISD food-establishment inspection violations, from Analyze Boston.
Source: `data/tmp7uhweub2.csv` (896,379 rows, raw) -> cleaned to
`data/food_inspections_clean.csv` (see `boston/clean_food_inspections.py`)
-> chunked to `final_data/food_inspections_chunks.json` (see
`boston/build_chunks.py`), **16,095 chunks** ready to embed.

## 2. Grain

The raw CSV is **one row = one violation citation within one inspection
visit** (`licenseno` + `resultdttm` + `violation` code). A single inspection
routinely produces 3-10+ rows. `licenseno` is a stable 1:1 key for
"one business at one physical address" — `businessname`, `address`,
`legalowner`, `licstatus`, `issdttm`/`expdttm` never vary within a license
(verified across the full dataset), and no license ever relocates.

**Chunks are per-license, chronological, ~1800-word-capped narratives**, not
per-row. Each chunk:
- Repeats a short identity header (business name, address, license,
  status) so it's self-contained for retrieval.
- Lists inspection events in date order, one line per violation.
- A business with a long history (max seen: 14 chunks / ~20k words of
  comments) is split into multiple sequential chunks
  (`chunk_index`/`total_chunks` in metadata); a single inspection visit
  never exceeds ~1600 words so events are never split mid-chunk.
- A "clean" inspection (no violations cited) still gets a one-line entry —
  don't assume gaps in the narrative mean missing data.

## 3. The fail -> pass linkage (read this before answering "did X fix Y")

The dataset's own pattern for "violation resolved" is: the same
`violation` code gets cited `Fail` on one inspection, then re-cited `Pass`
on the very next inspection **with the same comment text repeated**. The
chunk builder detects this and folds it into one line instead of embedding
the same sentence twice:

```
[2014-03-04] [FAIL] 17-4-302.14 Test Kit Provided (*): Provide a new test
kit for three bay sink... -> resolved (PASS) on 2014-03-11 (6 days later).
```

A `[FAIL]` line with **no** `-> resolved` suffix means it was never
re-cited Pass in this dataset — either still open, or the business closed/
went inactive before a follow-up. Check `metadata.has_unresolved_violation`
(true if any chunk lines are unresolved fails) before claiming a violation
was fixed.

`status_date` in the raw/clean CSV is **not** a separate confirmation
date — it's populated almost only on Pass rows and sits within ~0 days of
that row's own `resultdttm`. The real "resolved on" date used above is the
resolving Pass row's `resultdttm`, not `status_date`. Don't reach for
`status_date` expecting a distinct compliance-confirmation timestamp.

## 4. Metadata fields (on every chunk)

| Field | Meaning |
|---|---|
| `licenseno` | Stable business+location key. |
| `businessname`, `dbaname`, `legalowner` | Identity. `dbaname` is null 99% of the time (only set when operating under a different name) — absence is normal, not missing data. |
| `address`, `city`, `state`, `zip`, `zip4` | Zero-padded 5-digit `zip`; `zip4` holds the +4 extension when present (rare). |
| `lat`, `lon` | Parsed from the raw `location` field; all validated inside the Boston bounding box. |
| `property_id` | City parcel id. Can be null (17.6% of raw rows) — no `"0"` sentinel left, that's been nulled. |
| `licensecat` / `descript` | Establishment type: `FS`/"Eating & Drinking", `FT`/"Eating & Drinking w/ Take Out", `RF`/"Retail Food", `MFW`/"Mobile Food Walk On". |
| `licstatus` | `Active` / `Inactive` / `Deleted` — **current** status only (not point-in-time per inspection); an `Inactive` license can still have years of historical violation chunks. |
| `issdttm`, `expdttm` | License issue/expiration. ~24k rows in the raw data have `issdttm > expdttm` — a license-record quirk (fixed-date renewal cycles), not something to "fix" further. |
| `chunk_index`, `total_chunks` | Position within this license's chronological chunk sequence. |
| `period_start`, `period_end` | Date range of inspections covered by this chunk. |
| `num_inspections`, `num_violation_codes` | Counts within the chunk. |
| `violation_codes` | List of distinct violation codes appearing in this chunk — filter/join key into §5. |
| `has_unresolved_violation` | True if any fail in this chunk has no later pass in the data. |
| `label` | `"fail"` or `"pass"`, mirrors `has_unresolved_violation` — cheap SQL-side filter alongside the vector search. |

## 5. Violation code taxonomy (for building a lookup / filtering by topic)

Two coding schemes coexist in `violation`:
- **`NN-N-xxx` legacy Boston numeric codes** (~517k citations, majority) —
  e.g. `23-4-602.13`.
- **`590.xxx` codes** (~284k citations) — Massachusetts retail food code,
  105 CMR 590.000. Suffix tells you the category: `-P` = Priority
  (foodborne-illness risk factor), `-PF` = Priority Foundation (supports a
  priority item), `-C` = Core (general sanitation/operations).
- **`M-x-xxx`** (~26k) — manager/person-in-charge (PIC) duties and
  knowledge.
- **`L1`/`L2`** (~7k, `viol_level = "-"`, `violdesc` null) — administrative/
  licensing items (unpaid permit fee, missing allergen-awareness posting),
  not a food-safety code. Rendered in chunks as "Administrative/licensing
  item".

`viol_level` (`*` / `**` / `***`) tracks severity and lines up with the
MA Priority/Priority-Foundation/Core tiers above: `*` = minor/Core (586k,
most common), `**` = moderate/Priority-Foundation (114k), `***` =
severe/Priority (128k) — inferred from cross-referencing star level against
known-priority code suffixes, not from an explicit column, so treat it as a
strong pattern rather than a guaranteed 1:1 mapping.

**Top violations by frequency** (code — description):
| Code | Description | Citations |
|---|---|---|
| `23-4-602.13` | Non-Food Contact Surfaces Clean | 43,973 |
| `37-6-501.11-.12` | Improper Maintenance of Walls/Ceilings | 39,951 |
| `15-4-202.16` | Non-Food Contact Surfaces | 35,183 |
| `36-6-501.11-.12` | Improper Maintenance of Floors | 33,806 |
| `08-3-305-307.11` | Food Protection | 30,211 |
| `32-6-301.11-02.11` | Hand Cleaner, Drying, Tissue Signage | 23,325 |
| `42-6-501.113/.114` | Premises Maintained | 22,183 |
| `21-3-304.14` | Wiping Cloths, Clean, Sanitize | 18,261 |
| `29-5-201/02.11` | Installed and Maintained | 17,649 |
| `22-4-601/602.11` | Food Contact Surfaces Clean | 17,104 |
| `M-2-103.11` | PIC Performing Duties | 11,399 |
| `35-6-501.111/.115` | Insects, Rodents, Animals | 14,784 |
| `03-3-501.16(A)` | Cold Holding | 13,662 |
| `590.006/6-501.111-PF` | Controlling Pests (Pf) | 9,145 |

`violation` -> `violdesc` is a clean 1:1 mapping across all 458 codes (no
drift) — safe to treat `violation` as a stable categorical/lookup key.

## 6. Known data-quality gotchas

- **Zip codes**: source data had 122 rows with the leading zero dropped
  (`2127` instead of `02127`) and 308 rows in ZIP+4 format — both fixed in
  the clean CSV (§2 above); `zip` is always 5 digits or null.
- **`state` casing** (`MA`/`Ma`/`ma`) collapsed to `MA`.
- **`city`** had trailing-slash artifacts (`BRIGHTON/`) stripped, but a
  handful (~172 rows) are still genuinely compound (`ROXBURY/BOSTON`,
  `DOWNTOWN/FINANCIAL DISTRICT`) — not resolved to one canonical
  neighborhood, since the correct mapping isn't mechanical.
- **`result`** mixes two eras of coding (`Pass`/`Fail` pre-~2010 vs.
  `HE_Pass`/`HE_Fail`/`HE_FAILNOR`/`Failed` later) plus a `DATAERR`
  sentinel (42 rows). Chunk `label`/`has_unresolved_violation` are derived
  from `viol_status`, not raw `result` — prefer those over string-matching
  `result` yourself.
- **Test data leakage**: one raw row literally has `comments = "Test"` — it
  had a junk `viol_level` and was nulled out during cleaning, but if you're
  ever working from the raw CSV directly, expect stray rows like this.
- **`00000` zip rows** (86, harbor-island vendors like Spectacle Island
  concession stands) genuinely have no street zip — treated as null, not a
  bug.
- A business's `licstatus` is its **current** status, not a per-period
  snapshot — an `Inactive` business's older chunks describe a period when
  it may well have been active. Don't filter old chunks out just because
  `licstatus = Inactive`.

## 7. Retrieval tips

- **"Did [restaurant] fix [violation]?"** — retrieve chunks for that
  `licenseno`/business, look for the `-> resolved (PASS)` suffix on the
  relevant line; absence means still open as of the latest inspection in
  that chunk (check `total_chunks`/`chunk_index` — the answer might be in a
  later chunk).
- **"Which restaurants have unresolved violations?"** — filter
  `has_unresolved_violation = true` (or `label = 'fail'`), then vector
  search within that filtered set for the violation topic.
- **"How common is [topic, e.g. rats/mice]?"** — vector search
  `vector_text`/chunk text works better than code lookup here since pest
  language varies ("rats", "rodent droppings", "mice"); cross-check against
  `35-6-501.111/.115` (Insects, Rodents, Animals) in `violation_codes` for
  a precision check.
- **Geo/neighborhood questions** — use `lat`/`lon` or `city`, not `zip`
  alone (multiple neighborhoods can share a zip, and a few legit non-Boston
  zips exist for border properties — see §6 in the property-assessments
  doc for the same pattern).
- **Cross-dataset joins** — `property_id` here is the same City of Boston
  parcel id family as `PID` in `agent_md_files/property_assessments.md`,
  but isn't guaranteed populated (17.6% null) or formatted identically
  (unpadded here vs. zero-padded 10-digit `PID` there) — normalize before
  joining.

## 8. Quick facts

- 16,095 chunks across 11,297 licenses (businesses/locations).
- Chunk size: median 874 words, max 1,873 words (all under the 2,000-word
  target).
- 195,322 of 207,402 (license, violation-code) pairs recur — i.e. a
  fail-then-recheck cycle is the *normal* pattern in this data, not the
  exception.
- Data spans 2006-2026 (through the most recent inspections on file);
  ~40-56k violation rows/year in a typical full year.
