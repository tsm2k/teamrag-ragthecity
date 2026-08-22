"""
Embed a dataset of {"id", "text", "label", "metadata"} records (the shape
produced by export_for_pgvector.py / build_chunks.py, e.g. data/food_inspections.jsonl)
via OpenRouter, and dump the resulting vectors to final_data/ as JSONL.

Splitting embedding (slow, billed, needs OPENROUTER_API_KEY) from loading
(fast, needs Postgres) means the expensive step runs once and the pgvector
table can be rebuilt from the dump without re-calling the API.

Accepts either a JSON array (`--input foo.json`) or JSON Lines (`--input foo.jsonl`)
input file. Records with no non-empty "text" field are skipped -- that's the
"main data" worth embedding; everything else in the record just rides along
as metadata.

Requires:
    export OPENROUTER_API_KEY=sk-or-v1-...

Run:
    python -m chai_code.embed_dataset --input data/food_inspections.jsonl
    python -m chai_code.embed_dataset --input data/food_inspections.jsonl --limit 200   # smoke test
    python -m chai_code.embed_dataset --input final_data/food_inspections_chunks.json --output final_data/food_inspections_chunks_embedded.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterator

from boston.embed_openrouter import BATCH_SIZE, DEFAULT_MODEL, embed_texts

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "final_data"


def iter_raw_records(path: Path) -> Iterator[dict]:
    if path.suffix == ".jsonl":
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)
    else:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        yield from data


def iter_main_records(path: Path, limit: int | None) -> Iterator[dict]:
    """Yield only records that have text worth embedding, normalized to the
    id/text/label/metadata shape -- the "main data" for this dataset."""
    count = 0
    for row in iter_raw_records(path):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        yield {"id": row.get("id"), "text": text, "label": row.get("label"), "metadata": row.get("metadata")}
        count += 1
        if limit is not None and count >= limit:
            return


def embed_dataset(input_path: Path, output_path: Path, limit: int | None, model: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    batch: list[dict] = []

    with open(output_path, "w", encoding="utf-8") as out:
        def flush():
            nonlocal written
            if not batch:
                return
            embeddings = embed_texts([r["text"] for r in batch], model=model)
            for record, embedding in zip(batch, embeddings):
                out.write(json.dumps({**record, "embedding": embedding, "model": model}, ensure_ascii=False) + "\n")
            written += len(batch)
            print(f"  embedded {written:,}")
            batch.clear()

        for record in iter_main_records(input_path, limit):
            batch.append(record)
            if len(batch) >= BATCH_SIZE:
                flush()
        flush()

    if written == 0:
        raise SystemExit(f"No records with non-empty text found in {input_path}")
    print(f"Wrote {written:,} embedded records to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="JSON array or .jsonl of id/text/label/metadata records")
    parser.add_argument("--output", type=Path, default=None, help="default: final_data/<input stem>_embedded.jsonl")
    parser.add_argument("--limit", type=int, default=None, help="only embed the first N text-bearing records (smoke test)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    if not args.input.exists():
        raise SystemExit(f"{args.input} not found")

    output = args.output or (DEFAULT_OUTPUT_DIR / f"{args.input.stem}_embedded.jsonl")
    embed_dataset(args.input, output, args.limit, args.model)


if __name__ == "__main__":
    main()
