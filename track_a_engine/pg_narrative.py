"""Postgres-native hybrid narrative search: pg_search BM25 + pgvector dense,
fused with Reciprocal Rank Fusion, wired in as router.py's NarrativeLane.

WHY THIS EXISTS. NarrativeLane's docstring is deliberate: only the columns
that are actually prose (violation comments, permit work descriptions)
should be embedded -- see hybrid_search.py's own note that dense embeddings
fumble exact tokens (street names, business names) that BM25 nails. Chroma's
default embedder + Python-side rank_bm25 (hybrid_search.py) proves the point
at lab scale. This module is the same idea run on real infrastructure:
ParadeDB's pg_search gives real BM25 in SQL, pgvector gives real dense
vectors in the same table, and embed_openrouter.py supplies the embeddings
(a hosted model, not a local default) -- so one SQL query can rank both ways
and fuse them, instead of Python re-scoring an entire in-memory collection.

Prerequisites:
    boston.db must already have `inspection_violations` and `permits` tables
    (run `python -m boston.duck` first).
    export OPENROUTER_API_KEY=sk-or-v1-...   (only needed for `ingest`)
    ragcity-pg must be running with pg_search + vector installed (it is).

Run:
    .venv/bin/python -m track_a_engine.pg_narrative ingest   # embed + store + BM25-index
    .venv/bin/python -m track_a_engine.pg_narrative query --q "cooler failure"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from boston import duck  # noqa: E402
from boston.embed_openrouter import embed_texts, get_connection, store_embeddings  # noqa: E402

TABLE = "narrative_chunks"
RRF_K = 60
DEFAULT_K = 5

# Only the handful of columns that are actually free text -- everything else
# in these CSVs is codes, IDs, and categoricals that embed as noise.
SOURCES = [
    ("inspection_violations",
     """SELECT businessname || ' -- ' || comments AS text,
               'inspection: ' || businessname AS label,
               licenseno AS row_id
        FROM inspection_violations
        WHERE comments IS NOT NULL AND length(trim(comments)) > 15"""),
    ("permits",
     """SELECT worktype || ' -- ' || coalesce(description, '') || ' ' || coalesce(comments, '') AS text,
               'permit: ' || permitnumber AS label,
               permitnumber AS row_id
        FROM permits
        WHERE description IS NOT NULL AND length(trim(description)) > 15"""),
]


def ensure_bm25_index(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (f"{TABLE}_bm25_idx",))
        if cur.fetchone():
            return
        cur.execute(f"""
            CREATE INDEX {TABLE}_bm25_idx ON {TABLE}
            USING bm25 (id, text)
            WITH (key_field='id')
        """)
    conn.commit()


def ingest(limit_per_source: int) -> None:
    con = duck.connect()
    pg = get_connection()
    total = 0
    for dataset, sql in SOURCES:
        rows = con.execute(f"{sql} LIMIT {limit_per_source}").fetchall()
        cols = [d[0] for d in con.description]
        texts = [r[cols.index("text")] for r in rows]
        metas = [{"dataset": dataset, "row_id": str(r[cols.index("row_id")])} for r in rows]
        print(f"  embedding {len(texts)} rows from {dataset}…")
        ids = store_embeddings(pg, TABLE, texts=texts, labels=[dataset] * len(texts), metadatas=metas)
        total += len(ids)
    ensure_bm25_index(pg)
    pg.close()
    print(f"Stored {total} narrative chunks in Postgres table '{TABLE}', BM25-indexed.")


class PgHybridCollection:
    """Chroma-collection-shaped adapter so NarrativeLane needs no changes.

    .query() returns {"documents": [[...]], "metadatas": [[...]], "distances": [[...]]}
    just like chromadb.Collection.query(), ranked by RRF over BM25 + cosine.
    """

    def __init__(self, conn):
        self.conn = conn

    def count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {TABLE}")
            return cur.fetchone()[0]

    def query(self, query_texts: list[str], n_results: int = DEFAULT_K) -> dict:
        question = query_texts[0]
        pool = n_results * 4
        with self.conn.cursor() as cur:
            cur.execute(f"""
                SELECT id, text, label, metadata,
                       paradedb.score(id) AS bm25_score
                FROM {TABLE}
                WHERE text @@@ %s
                ORDER BY bm25_score DESC LIMIT %s
            """, (question, pool))
            bm25_rows = cur.fetchall()

            probe = embed_texts([question])[0]
            cur.execute(f"""
                SELECT id, text, label, metadata,
                       embedding <=> %s::vector AS distance
                FROM {TABLE}
                ORDER BY distance LIMIT %s
            """, (probe, pool))
            dense_rows = cur.fetchall()

        rrf: dict[int, float] = {}
        info: dict[int, tuple] = {}
        dense_distance: dict[int, float] = {}
        for rank, row in enumerate(bm25_rows):
            rid = row[0]
            rrf[rid] = rrf.get(rid, 0.0) + 1.0 / (RRF_K + rank)
            info[rid] = row
        for rank, row in enumerate(dense_rows):
            rid = row[0]
            rrf[rid] = rrf.get(rid, 0.0) + 1.0 / (RRF_K + rank)
            info[rid] = row
            dense_distance[rid] = row[4]

        top_ids = sorted(rrf, key=rrf.get, reverse=True)[:n_results]
        docs = [info[i][1] for i in top_ids]
        metas = [info[i][3] or {} for i in top_ids]
        # NarrativeLane only reads distances[0] as an abstain threshold (Chroma
        # cosine-distance scale) -- use the real dense distance where we have
        # it, else treat a BM25-only hit as an unremarkable middling distance.
        dists = [dense_distance.get(i, 1.0) for i in top_ids]
        return {"documents": [docs], "metadatas": [metas], "distances": [dists]}


def build_collection():
    """Entry point for router.py: returns a NarrativeLane-compatible collection,
    or None if Postgres/pg_search isn't reachable or nothing has been ingested."""
    try:
        conn = get_connection()
        coll = PgHybridCollection(conn)
        if coll.count() == 0:
            return None
        return coll
    except Exception:
        return None


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", help="Embed prose columns from boston.db into Postgres + BM25-index them")
    p_ingest.add_argument("--limit", type=int, default=500, help="rows per source table (keep OpenRouter bills sane)")
    p_query = sub.add_parser("query", help="Run a hybrid BM25+dense query")
    p_query.add_argument("--q", required=True)
    p_query.add_argument("--k", type=int, default=DEFAULT_K)
    args = ap.parse_args()

    if args.command == "ingest":
        ingest(args.limit)
    elif args.command == "query":
        coll = build_collection()
        if coll is None:
            raise SystemExit("No narrative chunks in Postgres yet — run `ingest` first.")
        res = coll.query([args.q], n_results=args.k)
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            print(f"[{dist:.3f}] ({meta.get('dataset')} {meta.get('row_id')}) {doc[:110]!r}")


if __name__ == "__main__":
    _cli()
