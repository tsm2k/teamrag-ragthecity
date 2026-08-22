"""
Build retrieval chunks from data/food_inspections_clean.csv for pgvector.

Grain change from export_for_pgvector.py: instead of one chunk per violation
row, this groups by licenseno (= one business at one location — verified
1:1 stable across the whole dataset: address/name/owner never change for a
given license) and produces a chronological narrative of that business's
inspection history, capped at ~1800 words per chunk so multi-year histories
split into several chunks instead of one giant blob.

Two things the raw rows don't give you directly, which this script derives:

1. Fail -> Pass resolution linking. The same violation code is very often
   cited FAIL on one inspection and then re-cited PASS on the very next
   inspection with the *same comment text* (that's how this dataset records
   "the restaurant fixed it"). Naively chunking would embed that comment
   twice. Instead we track open "episodes" per (license, violation code):
   the FAIL line gets an inline "-> resolved N days later on <date>"
   annotation, and the matching PASS row is dropped from the narrative
   instead of repeating the same text. A FAIL with no later PASS is left
   marked unresolved.

2. "No violations" inspections. ~61k rows in the cleaned CSV have no
   violation code at all (result_category=pass/not_required with nothing to
   cite) — these are real, correct rows, just placeholders meaning
   "inspection happened, nothing to report". They're folded into the
   narrative as one-line clean-inspection events instead of being dropped,
   so the fail/pass rhythm of a business's history stays intact.

Run:
    python boston/build_chunks.py
    python boston/build_chunks.py --limit-licenses 50   # smoke test
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "data" / "food_inspections_clean.csv"
DEFAULT_OUTPUT = REPO_ROOT / "final_data" / "food_inspections_chunks.json"

WORD_BUDGET = 1800  # narrative body target per chunk, before the header
HEADER_COLS = [
    "licenseno", "businessname", "dbaname", "legalowner", "address", "city",
    "state", "zip", "zip4", "property_id", "lat", "lon",
    "licensecat", "descript", "licstatus", "issdttm", "expdttm",
]


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, low_memory=False)
    for c in ["resultdttm", "violdttm", "status_date", "issdttm", "expdttm"]:
        df[c] = pd.to_datetime(df[c], errors="coerce", utc=True)
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    return df.sort_values(["licenseno", "resultdttm"], kind="stable")


def build_header(first_row: pd.Series) -> tuple[str, dict]:
    lines = [
        f"Business: {first_row['businessname']}"
        + (f" (dba {first_row['dbaname']})" if pd.notna(first_row.get("dbaname")) else ""),
        f"Address: {first_row['address']}, {first_row['city']}, {first_row['state']} {first_row['zip']}",
        f"License: {first_row['licenseno']} ({first_row['descript']}), status {first_row['licstatus']}",
    ]
    header_text = "\n".join(lines) + "\n---\n"
    header_meta = {c: (None if pd.isna(first_row.get(c)) else _jsonable(first_row.get(c))) for c in HEADER_COLS}
    return header_text, header_meta


def _jsonable(v):
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def render_events(license_rows: pd.DataFrame) -> list[dict]:
    """
    Returns a list of {"date": Timestamp, "text": str, "words": int,
    "violation_codes": set, "fail": bool} — one per inspection event
    (distinct resultdttm), in chronological order, with fail->pass episodes
    already resolved/annotated and redundant pass-of-a-fail rows dropped.
    """
    # pass 1: find, for every (violation code) FAIL row, the next PASS row
    # for the same code at this license -> that's the resolution.
    resolves_at = {}  # id(row) of the FAIL row -> resultdttm of the resolving PASS row
    drop_row = set()  # index of PASS rows that just close an episode (don't render separately)
    open_fail_idx: dict[str, int] = {}  # violation code -> index of the currently-open FAIL row

    coded = license_rows.dropna(subset=["violation"])
    for idx, row in coded.iterrows():
        code = row["violation"]
        if row["viol_status"] == "Fail":
            open_fail_idx[code] = idx
        elif row["viol_status"] == "Pass" and code in open_fail_idx:
            fail_idx = open_fail_idx.pop(code)
            resolves_at[fail_idx] = row["resultdttm"]
            drop_row.add(idx)

    events = []
    for resultdttm, day_rows in license_rows.groupby("resultdttm", sort=True):
        if pd.isna(resultdttm):
            continue
        lines = []
        codes = set()
        any_fail_open = False
        cited = day_rows.dropna(subset=["violation"])

        if cited.empty:
            result_cat = day_rows["result_category"].dropna().iloc[0] if day_rows["result_category"].notna().any() else "pass"
            lines.append(f"[{resultdttm.date()}] Inspection result: {result_cat.upper()} — no violations cited.")
        else:
            for idx, row in cited.iterrows():
                if idx in drop_row:
                    continue
                codes.add(row["violation"])
                status = row["viol_status"] if pd.notna(row["viol_status"]) else "?"
                desc = row["violdesc"] if pd.notna(row["violdesc"]) else "Administrative/licensing item"
                level = row["viol_level"] if pd.notna(row["viol_level"]) else ""
                comment = row["comments"] if pd.notna(row["comments"]) else ""
                line = f"[{resultdttm.date()}] [{status.upper()}] {row['violation']} {desc} ({level}): {comment}"
                if idx in resolves_at:
                    resolved_date = resolves_at[idx]
                    days = (resolved_date - resultdttm).days
                    line += f" -> resolved (PASS) on {resolved_date.date()} ({days} day{'s' if days != 1 else ''} later)."
                elif status == "Fail":
                    any_fail_open = True
                lines.append(line)

        if not lines:
            continue
        text = "\n".join(lines)
        events.append({
            "date": resultdttm,
            "text": text,
            "words": len(text.split()),
            "violation_codes": codes,
            "fail": any_fail_open,
        })
    return events


def pack_chunks(license_rows: pd.DataFrame) -> list[dict]:
    header_text, header_meta = build_header(license_rows.iloc[0])
    events = render_events(license_rows)

    if not events:
        return []

    chunks = []
    current: list[dict] = []
    current_words = 0

    def flush():
        nonlocal current, current_words
        if not current:
            return
        body = "\n".join(e["text"] for e in current)
        codes = sorted(set().union(*(e["violation_codes"] for e in current)))
        chunks.append({
            "text": header_text + body,
            "period_start": current[0]["date"],
            "period_end": current[-1]["date"],
            "num_inspections": len(current),
            "violation_codes": codes,
            "has_unresolved_violation": any(e["fail"] for e in current),
            "severity_label": "fail" if any(e["fail"] for e in current) else "pass",
        })
        current, current_words = [], 0

    for event in events:
        if current and current_words + event["words"] > WORD_BUDGET:
            flush()
        current.append(event)
        current_words += event["words"]
    flush()

    total = len(chunks)
    records = []
    for i, c in enumerate(chunks):
        meta = dict(header_meta)
        meta.update({
            "chunk_index": i,
            "total_chunks": total,
            "period_start": c["period_start"].isoformat(),
            "period_end": c["period_end"].isoformat(),
            "num_inspections": c["num_inspections"],
            "num_violation_codes": len(c["violation_codes"]),
            "violation_codes": c["violation_codes"],
            "has_unresolved_violation": c["has_unresolved_violation"],
        })
        records.append({
            "id": f"food_inspections-{meta['licenseno']}-chunk{i}",
            "text": c["text"],
            "label": c["severity_label"],
            "metadata": meta,
        })
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit-licenses", type=int, default=None, help="Only process the first N licenses (smoke testing)")
    args = parser.parse_args()

    df = load(args.input)
    license_ids = df["licenseno"].drop_duplicates()
    if args.limit_licenses:
        license_ids = license_ids.head(args.limit_licenses)

    all_records = []
    for lic in license_ids:
        rows = df[df["licenseno"] == lic]
        all_records.extend(pack_chunks(rows))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, separators=(",", ":"))

    word_counts = [len(r["text"].split()) for r in all_records]
    print(f"Licenses processed: {len(license_ids):,}")
    print(f"Chunks written: {len(all_records):,} -> {args.output}")
    if word_counts:
        print(f"Chunk word count: min={min(word_counts)} median={sorted(word_counts)[len(word_counts)//2]} "
              f"max={max(word_counts)} mean={sum(word_counts)/len(word_counts):.0f}")
        over = sum(1 for w in word_counts if w > 2000)
        print(f"Chunks over 2000 words: {over}")


if __name__ == "__main__":
    main()
