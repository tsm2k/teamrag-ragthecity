"""
Reusable helper: pull rows from Postgres, embed the text via OpenRouter,
store the vectors in a pgvector table, and query nearest neighbors.

Requires:
    export OPENROUTER_API_KEY=sk-or-v1-...
    export PG_DSN=postgresql://ragcity:ragcity@localhost:55432/ragcity   # optional, this is the default

As a library:
    from boston.embed_openrouter import get_connection, store_embeddings, nearest_neighbors

    conn = get_connection()
    ids = store_embeddings(conn, "my_table", texts=["a", "b"], labels=["x", "y"])
    nearest_neighbors(conn, "my_table", probe_text="something like a")

As a CLI:
    # embed the results of a SQL query into a table
    python -m boston.embed_openrouter store --table rat_comments \\
        --sql "SELECT DISTINCT comments AS text, 'rat' AS label FROM food_inspections WHERE comments ~* '\\yrats?\\y' LIMIT 10"

    # find nearest neighbors to some ad-hoc text already in a table
    python -m boston.embed_openrouter query --table rat_comments --probe "dead rat in the kitchen" --k 5
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Sequence

import psycopg2
import psycopg2.extras
import requests

DEFAULT_MODEL = "openai/text-embedding-3-large"
DEFAULT_PG_DSN = "postgresql://ragcity:ragcity@localhost:55432/ragcity"
OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
BATCH_SIZE = 100  # texts per OpenRouter call


def get_connection(dsn: str | None = None):
    return psycopg2.connect(dsn or os.environ.get("PG_DSN", DEFAULT_PG_DSN))


def embed_texts(texts: Sequence[str], model: str = DEFAULT_MODEL) -> list[list[float]]:
    """Embed a list of strings via OpenRouter, batching to stay under request limits."""
    api_key = os.environ["OPENROUTER_API_KEY"]
    embeddings: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        resp = requests.post(
            url=OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "model": model,
                "input": batch,
                "encoding_format": "float",
            }),
        )
        resp.raise_for_status()
        embeddings.extend(row["embedding"] for row in resp.json()["data"])
    return embeddings


def ensure_table(conn, table: str, dim: int) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id serial PRIMARY KEY,
                text text NOT NULL,
                label text,
                metadata jsonb,
                embedding vector({dim})
            )
            """
        )
    conn.commit()


def store_embeddings(
    conn,
    table: str,
    texts: Sequence[str],
    labels: Sequence[str] | None = None,
    metadatas: Sequence[dict] | None = None,
    model: str = DEFAULT_MODEL,
) -> list[int]:
    """Embed `texts` and insert them into `table` (created if missing). Returns the new row ids."""
    if not texts:
        return []
    labels = labels or [None] * len(texts)
    metadatas = metadatas or [None] * len(texts)

    embeddings = embed_texts(list(texts), model=model)
    ensure_table(conn, table, dim=len(embeddings[0]))

    ids = []
    with conn.cursor() as cur:
        for text, label, meta, emb in zip(texts, labels, metadatas, embeddings):
            cur.execute(
                f"""
                INSERT INTO {table} (text, label, metadata, embedding)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (text, label, json.dumps(meta) if meta is not None else None, emb),
            )
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def store_from_sql(conn, table: str, sql: str, model: str = DEFAULT_MODEL) -> list[int]:
    """
    Run `sql` (must return at least a `text` column, optionally `label`), embed
    the results, and store them in `table`.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    texts = [r["text"] for r in rows]
    labels = [r.get("label") for r in rows] if rows and "label" in rows[0] else None
    return store_embeddings(conn, table, texts, labels=labels, model=model)


def nearest_neighbors(
    conn,
    table: str,
    probe_text: str | None = None,
    probe_id: int | None = None,
    k: int = 5,
    model: str = DEFAULT_MODEL,
) -> list[tuple]:
    """
    Return the k nearest rows in `table` to either a fresh `probe_text` (embedded
    on the fly) or an existing row's `probe_id`, ordered by cosine distance.
    """
    if (probe_text is None) == (probe_id is None):
        raise ValueError("pass exactly one of probe_text or probe_id")

    with conn.cursor() as cur:
        if probe_text is not None:
            probe_embedding = embed_texts([probe_text], model=model)[0]
            cur.execute(
                f"""
                SELECT id, label, text, embedding <=> %s::vector AS distance
                FROM {table}
                ORDER BY distance
                LIMIT %s
                """,
                (probe_embedding, k),
            )
        else:
            cur.execute(
                f"""
                SELECT id, label, text, embedding <=> (SELECT embedding FROM {table} WHERE id = %s) AS distance
                FROM {table}
                WHERE id != %s
                ORDER BY distance
                LIMIT %s
                """,
                (probe_id, probe_id, k),
            )
        return cur.fetchall()


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_store = sub.add_parser("store", help="Embed the results of a SQL query and store them in a table")
    p_store.add_argument("--table", required=True)
    p_store.add_argument("--sql", required=True, help="Must return a 'text' column, optionally a 'label' column")
    p_store.add_argument("--model", default=DEFAULT_MODEL)

    p_query = sub.add_parser("query", help="Find nearest neighbors to a probe text or an existing row id")
    p_query.add_argument("--table", required=True)
    p_query.add_argument("--probe", help="Ad-hoc text to embed and search with")
    p_query.add_argument("--probe-id", type=int, help="Use an existing row's embedding as the probe")
    p_query.add_argument("--k", type=int, default=5)
    p_query.add_argument("--model", default=DEFAULT_MODEL)

    args = parser.parse_args()
    conn = get_connection()

    if args.command == "store":
        ids = store_from_sql(conn, args.table, args.sql, model=args.model)
        print(f"Stored {len(ids)} rows in {args.table}: ids {ids}")

    elif args.command == "query":
        try:
            rows = nearest_neighbors(
                conn, args.table, probe_text=args.probe, probe_id=args.probe_id, k=args.k, model=args.model
            )
        except ValueError as exc:
            parser.error(str(exc))
        for id_, label, text, distance in rows:
            print(f"[{distance:.4f}] ({label}) {text[:80]!r}")

    conn.close()


if __name__ == "__main__":
    _cli()
