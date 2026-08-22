"""
Load a JSONL dump of pre-computed embeddings (produced by boston/embed_dataset.py:
one {"id", "text", "label", "metadata", "embedding", "model"} object per line)
into a pgvector table -- no OpenRouter calls, this just inserts vectors that
already exist.

The original record "id" (e.g. "food_inspections-0") is folded into metadata
as "_source_id", since embed_openrouter.ensure_table's schema keys rows on
its own serial primary key rather than an external id.

Requires:
    export PG_DSN=postgresql://ragcity:ragcity@localhost:55432/ragcity   # optional, this is the default

Run:
    python -m chai_code.load_embeddings_to_pg --input final_data/food_inspections_embedded.jsonl --table food_inspections
    python -m chai_code.load_embeddings_to_pg --input ... --table ... --limit 200   # smoke test
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg2.extras

from boston.embed_openrouter import ensure_table, get_connection

INSERT_BATCH_SIZE = 500


def load(input_path: Path, table: str, limit: int | None) -> int:
    conn = get_connection()
    dim = None
    written = 0
    batch = []

    def flush():
        nonlocal written
        if not batch:
            return
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {table} (text, label, metadata, embedding) VALUES %s",
                batch,
            )
        conn.commit()
        written += len(batch)
        print(f"  loaded {written:,}")
        batch.clear()

    with open(input_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if dim is None:
                dim = len(row["embedding"])
                ensure_table(conn, table, dim=dim)

            metadata = dict(row.get("metadata") or {})
            metadata["_source_id"] = row.get("id")
            batch.append((row["text"], row.get("label"), json.dumps(metadata), row["embedding"]))

            if len(batch) >= INSERT_BATCH_SIZE:
                flush()
            if limit is not None and written + len(batch) >= limit:
                break
        flush()

    conn.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="JSONL dump from boston/embed_dataset.py")
    parser.add_argument("--table", required=True)
    parser.add_argument("--limit", type=int, default=None, help="only load the first N rows (smoke test)")
    args = parser.parse_args()

    if not args.input.exists():
        raise SystemExit(f"{args.input} not found -- run boston/embed_dataset.py first")

    written = load(args.input, args.table, args.limit)
    print(f"Loaded {written:,} rows into '{args.table}'")


if __name__ == "__main__":
    main()
