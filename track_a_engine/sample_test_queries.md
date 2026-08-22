# Sample test queries — property assessments (text-to-SQL) + food inspections (pgvector)

Test set for the agentic RAG (router → SQL lane over `boston_property_assessments_fy2024_2026.csv`,
vector/hybrid lane over `food_inspections.jsonl`). All "ground truth" values below were computed
directly against `final_data/boston_property_assessments_fy2024_2026.csv` and
`data/food_inspections_clean.csv` with DuckDB on 2026-08-22 — record the agent's actual answer next
to each and flag any mismatch or fabrication.

Legend — **Lane**: which retrieval path should fire. **Trap**: the specific failure mode this
question is designed to catch.

---

## Property assessments only (text-to-SQL)

### S1 — Simple: direct address lookup
**Q:** What is the total assessed value (FY2026) of the property at 195 Lexington St, East Boston?
**Lane:** SQL
**Ground truth:** PID `0100001000` → **$822,900** (up from $792,000 in FY2024, $799,000 in FY2025).
**Record:** answer __________ | correct ☐ | notes __________

### S2 — Simple: citywide aggregate
**Q:** What is the median total assessed property value across Boston for FY2026?
**Lane:** SQL
**Ground truth:** **$671,000** (across 184,552 non-null rows).
**Record:** answer __________ | correct ☐ | notes __________

### M1 — Medium: top-N with a definitional trap
**Q:** Which five Boston neighborhoods have the highest total FY2026 assessed property value, and how many buildings does each have?
**Lane:** SQL
**Trap:** `CITY = 'BOSTON'` is itself one of 19 neighborhood values (the downtown/general catch-all), not "all of Boston" — a correct answer shouldn't imply it means the whole city. Also should exclude/flag `IS_CONDO_MASTER = True` shell rows from the count (their $0 doesn't skew the sum, but including them in a building count is misleading).
**Ground truth (excl. condo-master rows):**
| Neighborhood | Total FY26 value | Buildings |
|---|---|---|
| BOSTON | $154.57B | 45,599 |
| DORCHESTER | $25.17B | 28,039 |
| SOUTH BOSTON | $17.85B | 14,179 |
| BRIGHTON | $14.42B | 12,017 |
| JAMAICA PLAIN | $11.51B | 10,929 |
**Record:** answer __________ | correct ☐ | notes __________

### M2 — Medium: known data-quality gotcha (two spellings, one category)
**Q:** How many parcels in Boston are classified as residential land (`LU = RL`)?
**Lane:** SQL
**Trap:** `LU` stores `"RL"` for 1 row and `"RL - RL"` for 5,923 rows — the same category. A literal `WHERE LU = 'RL'` returns 1 and silently misses 99.98% of the real answer.
**Ground truth:** **5,924** (`LU LIKE 'RL%'` or `LU IN ('RL','RL - RL')`).
**Record:** answer __________ | correct ☐ | notes __________

### M3 — Medium: derived-flag reasoning
**Q:** Which neighborhoods have the most properties flagged as rebuilt (`YR_BUILT` changed) between FY2024 and FY2026?
**Lane:** SQL
**Ground truth top 5:** Dorchester 55, East Boston 38, Boston 32, West Roxbury 21, Brighton 20 (259 citywide).
**Record:** answer __________ | correct ☐ | notes __________

### A1 — Advanced: condo reconstruction (multi-row join within one dataset)
**Q:** What is the total FY2026 assessed value of the condo building at 46 Rockvale Circle / 35–56 Lourdes Ave (GIS_ID `1102885000`), reconstructing the whole building from its individual condo units?
**Lane:** SQL
**Trap:** A naive single-PID lookup on `1102885000` alone returns **$0** — that PID is the `CONDO MAIN` association shell. The real value lives on the 14 sibling unit PIDs sharing that `GIS_ID` (`CM_ID = 1102885000`), and the master row must be excluded from the sum, not treated as "$0 = worthless."
**Ground truth:** **$7,450,000** (sum of 14 unit rows; master row excluded).
**Record:** answer __________ | correct ☐ | notes __________

---

## Food inspections only (vector / hybrid search)

### S3 — Simple: point lookup
**Q:** What violation was cited against 1000 Degrees Pizza on March 20, 2018, and what did the inspector's comment say?
**Lane:** Vector
**Ground truth:** Violation `13-2-304/402.11` — "Clean Cloths Hair Restraint." Comment: *"One staff person without hair restraint. Provide"* — result: Fail.
**Record:** answer __________ | correct ☐ | notes __________

### S4 — Simple: open semantic search
**Q:** Find inspection comments describing problems with refrigeration or walk-in coolers.
**Lane:** Vector
**Ground truth (open-ended, several valid hits):** e.g. "100 Percent Delicia Food" (635 Hyde Park Ave), 2015–2017: "Provide working internal thermometers for all refrigeration," "Clean interior and exterior of refrigerators... ice build up," "Raw foods stored above cooked foods in walk in refrigerator."
**Record:** answer __________ | correct ☐ | notes __________

### M3f — Medium: causal-invention trap (mirrors Lab 0 Stop 4)
**Q:** Why did [pick a business from S4's results]'s refrigeration equipment fail inspection — what was the mechanical cause?
**Lane:** Vector
**Trap:** The data holds violation codes/descriptions and inspector comments only (e.g. "visibly soiled," "ice build-up," "no thermometer") — it never states a root mechanical cause (compressor failure, power outage, etc.). Correct behavior: report what the comment actually says and explicitly decline to guess a cause. A fabricated mechanical explanation is a critical failure, not a partial credit answer.
**Record:** answer __________ | correct ☐ | notes __________

### M4 — Medium: entity spelling + grain trap
**Q:** How many failed inspections has Dunkin' Donuts had in Boston?
**Lane:** Vector (with an honest caveat) or abstain
**Trap:** Two stacked issues: (1) "Dunkin" spans 15+ distinct spellings in the data (`Dunkin Donuts`, `Dunkin' Donuts`, `DUNKIN'`, `DUNKIN DONUTS/GALLIVAN`, ...) totaling **16,201** rows matching `lower(businessname) LIKE '%dunkin%'` — an exact-match or single-spelling query silently undercounts by 70%+; (2) this source is **violation-grain, not inspection-grain** (one row per citation, not per visit) — the vector store has no per-inspection dedup, so any number produced overstates true inspection count by design. Correct behavior: either state both caveats explicitly or decline to give a single precise number, since a vector index cannot `GROUP BY` inspection to de-duplicate.
**Record:** answer __________ | correct ☐ | notes __________

### A2 — Advanced: structural abstention trap (aggregation on a vector-only source)
**Q:** Exactly how many failed food-safety violations has "The Real Deal" (1882 Centre St) racked up, broken down by year?
**Lane:** Vector — should abstain or heavily caveat
**Trap:** This is a pure counting/`GROUP BY year` question, but food inspections are ingested **only** as embedded text + metadata in pgvector — there is no SQL-queryable table for this dataset, so no lane can compute an exact aggregate. (For reference, violation-grain data shows ~1,166 total citation rows / ~734 fail-tagged at this address, but that already overstates true failed-inspection count per the M4 grain caveat, and a top-k vector query would only ever see a sample of it anyway.) Correct behavior: **abstain from a precise count/breakdown** rather than presenting retrieved-chunk counts as an exact aggregate — this is the "aggregation & counting" failure mode, hitting a genuinely vector-only source instead of a hypothetical one.
**Record:** answer __________ | correct ☐ | notes __________

---

## Multi-source (property SQL + food vector together)

### MS1 — Simple: same-address join
**Q:** What is the FY2026 assessed value of the building at 635 Hyde Park Ave (Roslindale), and has that address had any food-safety violations on record?
**Lane:** SQL + Vector
**Ground truth:** PID `1806741000`, `LU_DESC` = "RESTAURANT/Cafeteria," **TOTAL_VALUE_2026 = $658,000**. Yes — "100 Percent Delicia Food" has multiple violations there 2015–2017 (see S4).
**Record:** answer __________ | correct ☐ | notes __________

### MS2 — Medium: same-address join with a use-type nuance
**Q:** For "The Real Deal" restaurant at 1882 Centre St, West Roxbury — what's the assessed value of that property, and what land-use type is it assessed as?
**Lane:** SQL + Vector
**Trap:** The parcel is assessed `LU_DESC` = "STRIP CTR STORES" (a multi-tenant retail strip), not specifically "restaurant" — assessment land-use reflects the building/parcel, not the individual tenant business. An answer that says "assessed as a restaurant" is wrong.
**Ground truth:** PID `2005766000`, **TOTAL_VALUE_2026 = $578,900**, `LU_DESC` = "STRIP CTR STORES."
**Record:** answer __________ | correct ☐ | notes __________

### MS3 — Advanced: ID-space mismatch trap (the real test of the multi-source join)
**Q:** Using the `property_id` recorded on a food inspection record, look up that property in the property-assessment table to get its owner and assessed value.
**Lane:** should recognize it cannot join directly
**Trap:** `food_inspections.metadata.property_id` (e.g. `"156152"`, `"18399"`) comes from a **different ID system** than `assessment.PID` (10-digit zero-padded parcel IDs). Verified: **zero overlap** across all 4,491 distinct food-inspection `property_id` values, even after zero-padding to 10 digits. This mirrors the documented `permits.property_id` vs `assessment.pid` mismatch. A correct agent recognizes the ID spaces don't correspond and either says so or falls back to address-based matching — silently running the join and returning an empty/wrong result as if it were a real answer is the failure to catch.
**Record:** answer __________ | correct ☐ | notes __________

### MS4 — Advanced: open-ended two-hop reasoning
**Q:** List Boston restaurants in Dorchester with a food-safety violation mentioning "mice" or "rodent" droppings whose building is assessed above $700,000 in FY2026.
**Lane:** Vector (find candidates) → SQL (filter by value)
**Ground truth (candidate pool before the value filter; verify the $700K cut with SQL):** Planet Gracie (164 Blue Hill Ave), Brother's Crawfish (272 Adams St), New Garden Restaurant (746 Dudley St), New York Fried Chicken (251 Bowdoin St), La La Restaurant (792 Washington St), Dorchester Food Co-op (195 Bowdoin St), Speedway No. 2430 (820 Columbia Rd), Home Run Cafe (1269 Massachusetts Ave).
**Record:** answer __________ | correct ☐ | notes __________

---

## Scoring notes
- Anything with a numeric "Ground truth" above should match exactly (or be explicitly caveated the same way the trap describes) — a confident but different number is a fabrication, not partial credit.
- M3f and A2 have **no correct numeric/causal answer** — the only pass condition is that the agent declines rather than invents one.
- MS3 has **no correct join result** — the only pass condition is that the agent doesn't silently return a wrong/empty answer as if the join were valid.
