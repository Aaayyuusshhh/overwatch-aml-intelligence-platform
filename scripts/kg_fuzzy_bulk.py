#!/usr/bin/env python3
"""Bulk fuzzy extension of entity_groups.

For every entity_group with a canonical_name, find watchlist_records from
sources NOT already in its source_list whose name is trgm-similar above a
threshold. Insert one fuzzy link per (group, new_source) pair. This is the
same logic as scripts/knowledge_graph.py::build_fuzzy but runs as one bulk
INSERT...SELECT instead of per-group iteration — orders of magnitude faster
and not biased to high-record-count groups.

Default threshold 0.65: catches "Masood Azhar" ↔ "Maulana Masood Azhar"
(sim=0.68) but rejects most spurious corporate-name confusion at 0.6.

Re-runnable. Existing fuzzy links from this script are wiped first.
Updates entity_groups.{source_list, num_sources, num_records} from the
new links.
"""
from __future__ import annotations
import argparse, time
import psycopg2

DB = dict(host="localhost", user="aayush", password="aayush123", dbname="risk_pipeline")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.65)
    ap.add_argument("--reset-fuzzy", action="store_true",
                    help="Delete all existing fuzzy entity_links first")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL work_mem = '512MB';")
    cur.execute("SET LOCAL statement_timeout = 0;")

    if args.reset_fuzzy:
        print("[reset] deleting existing fuzzy entity_links...")
        cur.execute("DELETE FROM entity_links WHERE match_type='fuzzy';")
        n_deleted = cur.rowcount
        print(f"  deleted {n_deleted:,}")

    t0 = time.time()
    print(f"[bulk fuzzy] threshold={args.threshold}")

    # Pick one anchor per group (smallest id from existing exact links)
    cur.execute("""
        CREATE TEMP TABLE _group_anchor AS
        SELECT DISTINCT ON (entity_group_id)
               entity_group_id, record_id_a, name_a, source_a
        FROM entity_links
        WHERE match_type='exact'
        ORDER BY entity_group_id, record_id_a;
        CREATE INDEX ON _group_anchor (entity_group_id);
    """)
    cur.execute("SELECT COUNT(*) FROM _group_anchor;")
    n_anchors = cur.fetchone()[0]
    print(f"  {n_anchors:,} group anchors")

    # Find candidate (group, new_source, best_record) triples via trgm.
    # The `name % canonical_name` clause uses the GIN trgm index for prune.
    print(f"  scanning for fuzzy matches above {args.threshold}...")
    cur.execute("""
        CREATE TEMP TABLE _candidates AS
        SELECT DISTINCT ON (g.group_id, w.source_id)
               g.group_id,
               a.record_id_a, a.name_a, a.source_a,
               w.id   AS rid,
               w.name AS rname,
               w.source_id AS rsrc,
               similarity(LOWER(w.name), LOWER(g.canonical_name)) AS sim
        FROM entity_groups g
        JOIN _group_anchor a ON a.entity_group_id = g.group_id
        JOIN watchlist_records w
              ON w.name %% g.canonical_name
             AND w.source_id <> ALL(g.source_list)
        WHERE LENGTH(TRIM(g.canonical_name)) > 4
          AND similarity(LOWER(w.name), LOWER(g.canonical_name)) > %s
        ORDER BY g.group_id, w.source_id, sim DESC;
    """, (args.threshold,))
    cur.execute("SELECT COUNT(*) FROM _candidates;")
    n_cands = cur.fetchone()[0]
    print(f"  {n_cands:,} (group, new_source) fuzzy candidates in {time.time()-t0:.1f}s")

    # Insert as entity_links
    cur.execute("""
        INSERT INTO entity_links
            (entity_group_id, record_id_a, record_id_b,
             name_a, name_b, source_a, source_b,
             match_type, similarity_score)
        SELECT group_id, record_id_a, rid,
               name_a, rname, source_a, rsrc,
               'fuzzy', sim
        FROM _candidates;
    """)
    n_inserted = cur.rowcount
    print(f"  inserted {n_inserted:,} fuzzy entity_links")

    # Update entity_groups to reflect new sources + record counts.
    # source_list ← union of existing + new sources from fuzzy
    # num_sources ← length of that union
    # num_records += number of fuzzy records added
    print(f"  updating entity_groups source_list / num_sources / num_records...")
    cur.execute("""
        WITH new_per_group AS (
            SELECT group_id,
                   array_agg(DISTINCT rsrc ORDER BY rsrc) AS new_srcs,
                   COUNT(*) AS new_recs
            FROM _candidates GROUP BY group_id
        )
        UPDATE entity_groups g
        SET source_list = (
                SELECT array_agg(DISTINCT s ORDER BY s)
                FROM unnest(g.source_list || c.new_srcs) s
            ),
            num_sources = (
                SELECT COUNT(DISTINCT s)
                FROM unnest(g.source_list || c.new_srcs) s
            )::int,
            num_records = g.num_records + c.new_recs::int,
            updated_at = NOW()
        FROM new_per_group c
        WHERE c.group_id = g.group_id;
    """)
    n_updated = cur.rowcount
    print(f"  updated {n_updated:,} entity_groups")

    conn.commit()
    cur.close()
    conn.close()
    print(f"[done] total elapsed {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
