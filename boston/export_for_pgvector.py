"""
Convert data/food_inspections_clean.csv into JSON Lines shaped for pgvector
ingestion: one {"id", "text", "label", "metadata"} object per row, matching
the texts/labels/metadatas signature that embed_openrouter.store_embeddings()
expects.

  text     -> vector_text (the comment to embed; falls back to violdesc)
  label    -> result_category (pass/fail/pending/not_required/closed)
  metadata -> every other column, as a JSON object (numbers coerced,
              blanks dropped so the jsonb stays small)

Streams the CSV row by row (csv.DictReader) instead of loading it into a
DataFrame, since the cleaned file is ~500MB.

Run:
    python boston/export_for_pgvector.py
    python boston/export_for_pgvector.py --limit 1000        # smoke test
    python boston/export_for_pgvector.py --include-empty     # keep rows
                                                               # with no text,
                                                               # using a
                                                               # placeholder

Then load it, e.g.:
    import json
    from boston.embed_openrouter import get_connection, store_embeddings
    conn = get_connection()
    texts, labels, metas = [], [], []
    for line in open("data/food_inspections.jsonl"):
        row = json.loads(line)
        texts.append(row["text"]); labels.append(row["label"]); metas.append(row["metadata"])
    store_embeddings(conn, "food_inspections", texts, labels=labels, metadatas=metas)
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "data" / "food_inspections_clean.csv"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "food_inspections.jsonl"
TEXT_COL = "vector_text"
LABEL_COL = "result_category"
EMPTY_TEXT_PLACEHOLDER = "Inspection recorded with no violation cited."

# columns that are numeric in the cleaned CSV and should be emitted as
# JSON numbers rather than strings
NUMERIC_COLS = {"lat", "lon"}


def row_to_record(row: dict, row_id: int, include_empty: bool) -> dict | None:
    text = (row.get(TEXT_COL) or "").strip()
    if not text:
        if not include_empty:
            return None
        text = EMPTY_TEXT_PLACEHOLDER

    metadata = {}
    for key, value in row.items():
        if key == TEXT_COL or value is None:
            continue
        value = value.strip()
        if value == "":
            continue  # drop blanks instead of storing "" in jsonb
        if key in NUMERIC_COLS:
            try:
                value = float(value)
            except ValueError:
                pass
        metadata[key] = value

    return {
        "id": f"food_inspections-{row_id}",
        "text": text,
        "label": row.get(LABEL_COL) or None,
        "metadata": metadata,
    }


def convert(input_path: Path, output_path: Path, limit: int | None, include_empty: bool) -> None:
    csv.field_size_limit(10_000_000)
    written = skipped_empty = 0

    with open(input_path, newline="", encoding="utf-8") as src, open(output_path, "w", encoding="utf-8") as dst:
        reader = csv.DictReader(src)
        for row_id, row in enumerate(reader):
            if limit is not None and written >= limit:
                break
            record = row_to_record(row, row_id, include_empty)
            if record is None:
                skipped_empty += 1
                continue
            dst.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    print(f"Wrote {written:,} records to {output_path}")
    print(f"Skipped {skipped_empty:,} rows with no comments/violdesc text"
          + ("" if include_empty else " (pass --include-empty to keep them with a placeholder)"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None, help="Only convert the first N rows (smoke testing)")
    parser.add_argument("--include-empty", action="store_true",
                         help="Keep rows with no comments/violdesc, using a placeholder string")
    args = parser.parse_args()

    if not args.input.exists():
        raise SystemExit(f"{args.input} not found — run boston/clean_food_inspections.py first")

    convert(args.input, args.output, args.limit, args.include_empty)


if __name__ == "__main__":
    main()
