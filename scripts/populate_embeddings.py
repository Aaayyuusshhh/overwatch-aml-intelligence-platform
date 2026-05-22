"""Populate name_embedding vectors for watchlist_records.

Strategy: focus on names that appear in entity_groups (i.e. canonical
multi-source entities ~362k unique). Embed those once, then UPDATE all
watchlist_records rows whose LOWER(TRIM(name)) matches. Full 3.78M unique
names would need ~30 min of CPU time + 5+ GB of RAM and is not worth it
for a first useful prototype — semantic search is most valuable across
the dedup'd / cross-source entities anyway.

Model: all-MiniLM-L6-v2 (384-dim, CPU-friendly).
Pass --all to override and embed every distinct watchlist name (slow).
"""
from __future__ import annotations
import argparse, os, sys, time
import psycopg2
from psycopg2.extras import execute_values

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB = {
    "host": os.environ.get("PGHOST", "localhost"),
    "user": os.environ.get("PGUSER", "aayush"),
    "password": os.environ.get("PGPASSWORD", "aayush123"),
    "dbname": os.environ.get("PGDATABASE", "risk_pipeline"),
}

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
BATCH_SIZE = 512


def fetch_names(cur, mode: str, limit: int | None) -> list[str]:
    if mode == "groups":
        sql = """
            SELECT DISTINCT LOWER(TRIM(canonical_name))
            FROM entity_groups
            WHERE canonical_name IS NOT NULL
              AND LENGTH(TRIM(canonical_name)) > 2
        """
    elif mode == "high":
        sql = """
            SELECT DISTINCT LOWER(TRIM(canonical_name))
            FROM entity_groups
            WHERE canonical_name IS NOT NULL
              AND LENGTH(TRIM(canonical_name)) > 2
              AND risk_level = 'HIGH'
        """
    else:  # all
        sql = """
            SELECT DISTINCT LOWER(TRIM(name))
            FROM watchlist_records
            WHERE LENGTH(TRIM(name)) > 2
              AND name_embedding IS NULL
        """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["groups", "high", "all"], default="groups",
                    help="which names to embed: groups (canonical names from "
                         "entity_groups), high (HIGH-risk only), or all "
                         "(every distinct watchlist name)")
    ap.add_argument("--limit", type=int, help="cap on number of distinct names")
    args = ap.parse_args()

    t0 = time.time()
    print(f"Loading model {MODEL_NAME}...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    print(f"  loaded in {time.time()-t0:.1f}s")

    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    cur = conn.cursor()

    print(f"Fetching names (mode={args.mode}, limit={args.limit})...")
    names = fetch_names(cur, args.mode, args.limit)
    print(f"  {len(names):,} unique names")
    if not names:
        print("Nothing to embed.")
        return

    cur.execute("""
        DROP TABLE IF EXISTS _name_embeddings;
        CREATE TEMP TABLE _name_embeddings (
            clean_name TEXT PRIMARY KEY,
            embedding vector(384)
        );
    """)

    print("Embedding...")
    t_enc = time.time()
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i:i+BATCH_SIZE]
        vectors = model.encode(batch, show_progress_bar=False,
                               normalize_embeddings=True, batch_size=BATCH_SIZE)
        rows = [(n, "[" + ",".join(f"{v:.6f}" for v in vec) + "]")
                for n, vec in zip(batch, vectors)]
        execute_values(cur,
            "INSERT INTO _name_embeddings (clean_name, embedding) VALUES %s "
            "ON CONFLICT (clean_name) DO NOTHING",
            rows, page_size=500)
        done = min(i + BATCH_SIZE, len(names))
        if (i // BATCH_SIZE) % 20 == 0:
            elapsed = time.time() - t_enc
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(names) - done) / rate if rate > 0 else 0
            print(f"  {done:>7,}/{len(names):,}  rate={rate:>4.0f}/s  eta={eta:.0f}s")
    print(f"  encoded {len(names):,} names in {time.time()-t_enc:.1f}s")

    print("Updating watchlist_records...")
    t_upd = time.time()
    cur.execute("""
        UPDATE watchlist_records w
        SET name_embedding = e.embedding
        FROM _name_embeddings e
        WHERE LOWER(TRIM(w.name)) = e.clean_name
          AND w.name_embedding IS NULL;
    """)
    n_updated = cur.rowcount
    print(f"  updated {n_updated:,} rows in {time.time()-t_upd:.1f}s")

    conn.commit()
    cur.execute("SELECT COUNT(*), COUNT(name_embedding) FROM watchlist_records;")
    total, with_emb = cur.fetchone()
    print(f"\nFinal: {with_emb:,}/{total:,} records have embeddings ({with_emb*100//total}%)")
    print(f"Total elapsed: {time.time()-t0:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
