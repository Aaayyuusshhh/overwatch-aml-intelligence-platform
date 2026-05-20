"""
Entity-linking knowledge graph over watchlist_records.

Commands:
  --build-exact   Cross-source exact name matches -> entity_groups + entity_links
  --build-fuzzy   Fuzzy (pg_trgm) extension of single-source groups (slow; opt-in)
  --risk-score    Derive risk_level / risk_signals from each group's source list
  --stats         Print summary of groups / risk levels / top entities
  --search NAME   Find an entity across sources (exact + fuzzy)
  --reset         Drop existing entity_groups/entity_links rows before --build-exact

Design notes
------------
* All heavy work is done inside PostgreSQL via INSERT...SELECT. We never load
  the 4.77M records into Python memory.
* entity_links uses a "star" pattern: per group we pick one anchor record
  (lowest id), then create one link from that anchor to one representative
  record from each OTHER source. A name in 32 sources -> 31 links (not 32C2=496).
* Junk filtering is applied at the SQL level (length, pure-numeric/punct, a
  blacklist of common stop-strings like 'unknown', 'n/a', etc.).
"""
import argparse
import os
import sys
import time

import psycopg2
import psycopg2.extras

DB_CONFIG = {
    "host":     os.environ.get("PGHOST", "localhost"),
    "port":     int(os.environ.get("PGPORT", 5432)),
    "dbname":   os.environ.get("PGDATABASE", "risk_pipeline"),
    "user":     os.environ.get("PGUSER", "aayush"),
    "password": os.environ.get("PGPASSWORD", "aayush123"),
}

# Common junk values seen in name fields across sources.
# Bearer/Person come from offshore-leak corporate registries (placeholder
# entities for bearer-share companies and unnamed officers).
NAME_BLACKLIST = (
    "not available", "n/a", "na", "unknown", "none", "null", "test",
    "-", "--", "---", "tba", "tbd", "to be advised", "see notes",
    "see remarks", "anonymous", "no name", "various", "see attached",
    "the bearer", "bearer", "bearer 1", "person", "individual",
    "company", "entity", "limited", "ltd",
)

# Risk keyword patterns. Matched case-insensitively against each element
# in source_list and against the source_agency string. PG regex syntax.
HIGH_PATTERN   = r"wanted|sanction|fugitive|terror|banned|red[-_]?notice|interpol|nia_|mha_|ofac|un_|cbi_|ed_"
MEDIUM_PATTERN = r"defaulter|debarred|suspend|enforc|penalt|disqualif|revok|disciplin|caution|warn|alert|prohibit|cancel"

DDL = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS entity_groups (
    id SERIAL PRIMARY KEY,
    group_id UUID UNIQUE NOT NULL,
    canonical_name TEXT,
    num_records INTEGER,
    num_sources INTEGER,
    source_list TEXT[],
    risk_level TEXT,
    risk_signals TEXT[],
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_entity_groups_canonical_lower
    ON entity_groups (LOWER(TRIM(canonical_name)));
CREATE INDEX IF NOT EXISTS idx_entity_groups_risk ON entity_groups (risk_level);

CREATE TABLE IF NOT EXISTS entity_links (
    id SERIAL PRIMARY KEY,
    entity_group_id UUID NOT NULL,
    record_id_a INTEGER REFERENCES watchlist_records(id) ON DELETE CASCADE,
    record_id_b INTEGER REFERENCES watchlist_records(id) ON DELETE CASCADE,
    name_a TEXT,
    name_b TEXT,
    source_a TEXT,
    source_b TEXT,
    match_type TEXT,
    similarity_score FLOAT,
    confirmed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_entity_links_group   ON entity_links (entity_group_id);
CREATE INDEX IF NOT EXISTS idx_entity_links_records ON entity_links (record_id_a, record_id_b);
"""

# SQL fragment used in several queries: the "interesting" rows from
# watchlist_records — name non-empty, length>3, not pure punct/digits,
# not on the blacklist.
INTERESTING_WHERE = """
    name IS NOT NULL
    AND LENGTH(TRIM(name)) > 3
    AND TRIM(name) !~ '^[0-9[:space:][:punct:]]+$'
    AND LOWER(TRIM(name)) <> ALL(%(blacklist)s)
"""


def connect():
    return psycopg2.connect(**DB_CONFIG)


def ensure_schema(cur):
    cur.execute(DDL)


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ build-exact

def build_exact(reset: bool):
    blacklist = list(NAME_BLACKLIST)
    with connect() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            ensure_schema(cur)
            if reset:
                _log("RESET: truncating entity_links and entity_groups...")
                cur.execute("TRUNCATE entity_links, entity_groups RESTART IDENTITY;")

            # Bump work_mem for the big GROUP BY and DISTINCT ON.
            cur.execute("SET LOCAL work_mem = '512MB';")

            # Step 1: insert one row per cross-source-shared normalized name.
            _log("Step 1/2: building entity_groups (GROUP BY LOWER(TRIM(name)))...")
            t0 = time.time()
            cur.execute(f"""
                INSERT INTO entity_groups
                    (group_id, canonical_name, num_records, num_sources,
                     source_list, first_seen, last_seen)
                SELECT gen_random_uuid(),
                       (array_agg(name ORDER BY id))[1] AS canonical,
                       COUNT(*)::int,
                       COUNT(DISTINCT source_id)::int,
                       array_agg(DISTINCT source_id ORDER BY source_id),
                       MIN(loaded_at),
                       MAX(loaded_at)
                FROM watchlist_records
                WHERE {INTERESTING_WHERE}
                GROUP BY LOWER(TRIM(name))
                HAVING COUNT(DISTINCT source_id) >= 2
                ON CONFLICT (group_id) DO NOTHING;
            """, {"blacklist": blacklist})
            n_groups = cur.rowcount
            _log(f"  inserted {n_groups:,} entity_groups in {time.time()-t0:.1f}s")

            # Step 2: build entity_links rows. For each group:
            #   - "anchor" = record with the smallest id
            #   - "rep(source)" = one record per (group, source) with smallest id
            # Create a link from the anchor to every rep that is from a different source.
            _log("Step 2/2: building entity_links (star pattern; one rep per source)...")
            t0 = time.time()
            cur.execute(f"""
                WITH normalized AS (
                    SELECT id, source_id, name, LOWER(TRIM(name)) AS nm
                    FROM watchlist_records
                    WHERE {INTERESTING_WHERE}
                ),
                shared AS (
                    SELECT nm
                    FROM normalized
                    GROUP BY nm
                    HAVING COUNT(DISTINCT source_id) >= 2
                ),
                rep AS (
                    -- one canonical record per (nm, source_id): smallest id wins
                    SELECT DISTINCT ON (n.nm, n.source_id)
                           n.id, n.source_id, n.name, n.nm
                    FROM normalized n
                    JOIN shared s USING (nm)
                    ORDER BY n.nm, n.source_id, n.id
                ),
                anchor AS (
                    -- smallest-id rep per group becomes the anchor
                    SELECT DISTINCT ON (nm) id AS aid, source_id AS asrc, name AS aname, nm
                    FROM rep
                    ORDER BY nm, id
                )
                INSERT INTO entity_links
                    (entity_group_id, record_id_a, record_id_b,
                     name_a, name_b, source_a, source_b,
                     match_type, similarity_score)
                SELECT g.group_id, a.aid, r.id, a.aname, r.name, a.asrc, r.source_id,
                       'exact', 1.0
                FROM entity_groups g
                JOIN anchor a ON a.nm = LOWER(TRIM(g.canonical_name))
                JOIN rep r    ON r.nm = a.nm
                WHERE r.source_id <> a.asrc;
            """, {"blacklist": blacklist})
            n_links = cur.rowcount
            _log(f"  inserted {n_links:,} entity_links in {time.time()-t0:.1f}s")

            conn.commit()
            _log("build-exact: COMMIT")

    # Quick re-stat summary
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM entity_groups;")
        n_g = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM entity_links WHERE match_type='exact';")
        n_l = cur.fetchone()[0]
        cur.execute("""
            SELECT num_sources, COUNT(*) FROM entity_groups
            GROUP BY num_sources ORDER BY num_sources DESC LIMIT 8;
        """)
        rows = cur.fetchall()
    _log(f"after build-exact: groups={n_g:,} links={n_l:,}")
    _log("source-count distribution (top buckets):")
    for ns, c in rows:
        print(f"    {ns:3d} sources : {c:,} groups")


# -------------------------------------------------------------------- build-fuzzy

def build_fuzzy(limit: int = 10_000, threshold: float = 0.7):
    """For groups currently spanning only 1 source, try pg_trgm fuzzy matching
    against names in OTHER sources. Slow — bounded by --limit."""
    blacklist = list(NAME_BLACKLIST)
    _log(f"build-fuzzy: scanning up to {limit:,} single-source groups, threshold={threshold}")
    with connect() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            ensure_schema(cur)
            cur.execute("SET LOCAL work_mem = '512MB';")
            # nominate candidate groups (single-source) — but only those whose
            # name is interesting (length, not blacklist). We treat the
            # entity_groups themselves as canonical and look for fuzzy matches
            # in watchlist_records.
            cur.execute(f"""
                SELECT g.group_id, g.canonical_name, (g.source_list)[1] AS only_src
                FROM entity_groups g
                WHERE g.num_sources = 1
                  AND LENGTH(TRIM(g.canonical_name)) > 4
                  AND LOWER(TRIM(g.canonical_name)) <> ALL(%(blacklist)s)
                ORDER BY LENGTH(g.canonical_name) DESC
                LIMIT %(limit)s;
            """, {"blacklist": blacklist, "limit": limit})
            cands = cur.fetchall()
            _log(f"  {len(cands):,} candidate single-source groups")

            n_links = 0
            t0 = time.time()
            for i, (gid, name, only_src) in enumerate(cands, 1):
                cur.execute("""
                    SELECT id, name, source_id,
                           similarity(LOWER(name), LOWER(%s)) AS sim
                    FROM watchlist_records
                    WHERE source_id <> %s
                      AND name %% %s
                      AND LOWER(LEFT(TRIM(name),3)) = LOWER(LEFT(TRIM(%s),3))
                      AND similarity(LOWER(name), LOWER(%s)) > %s
                    ORDER BY sim DESC
                    LIMIT 5;
                """, (name, only_src, name, name, name, threshold))
                rows = cur.fetchall()
                if not rows:
                    continue
                # Use the first known anchor record from entity_links for record_id_a
                cur.execute("""
                    SELECT record_id_a, name_a, source_a FROM entity_links
                    WHERE entity_group_id = %s LIMIT 1;
                """, (gid,))
                anchor = cur.fetchone()
                if not anchor:
                    # single-source group has no link rows yet (only one record).
                    # Pick any matching record from the group's only source.
                    cur.execute("""
                        SELECT id, name, source_id FROM watchlist_records
                        WHERE source_id = %s AND LOWER(TRIM(name)) = LOWER(TRIM(%s))
                        ORDER BY id LIMIT 1;
                    """, (only_src, name))
                    anchor = cur.fetchone()
                    if not anchor:
                        continue
                aid, aname, asrc = anchor
                ins = []
                for rid, rname, rsrc, sim in rows:
                    ins.append((str(gid), aid, rid, aname, rname, asrc, rsrc, "fuzzy", float(sim)))
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO entity_links
                       (entity_group_id, record_id_a, record_id_b,
                        name_a, name_b, source_a, source_b,
                        match_type, similarity_score) VALUES %s""",
                    ins,
                )
                n_links += len(ins)
                if i % 500 == 0:
                    _log(f"  scanned {i:,}/{len(cands):,} groups, added {n_links:,} fuzzy links")
            conn.commit()
            _log(f"build-fuzzy done: added {n_links:,} fuzzy links in {time.time()-t0:.1f}s")


# --------------------------------------------------------------------- risk-score

def risk_score():
    _log("risk-score: assigning risk_level + risk_signals to each entity_group")
    with connect() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            ensure_schema(cur)
            # Single UPDATE: regex-match source_list items + source_agency strings.
            cur.execute(f"""
                WITH agency AS (
                    SELECT g.group_id,
                           array_agg(DISTINCT w.source_agency) FILTER (WHERE w.source_agency IS NOT NULL) AS agencies
                    FROM entity_groups g
                    JOIN watchlist_records w ON LOWER(TRIM(w.name)) = LOWER(TRIM(g.canonical_name))
                    GROUP BY g.group_id
                ),
                signals AS (
                    SELECT g.group_id,
                           ARRAY(
                             SELECT DISTINCT s FROM unnest(g.source_list || COALESCE(a.agencies, '{{}}'::text[])) s
                             WHERE s ~* %(high)s OR s ~* %(med)s
                           ) AS sig,
                           (EXISTS (SELECT 1 FROM unnest(g.source_list || COALESCE(a.agencies, '{{}}'::text[])) s WHERE s ~* %(high)s)) AS is_high,
                           (EXISTS (SELECT 1 FROM unnest(g.source_list || COALESCE(a.agencies, '{{}}'::text[])) s WHERE s ~* %(med)s))  AS is_med
                    FROM entity_groups g LEFT JOIN agency a USING (group_id)
                )
                UPDATE entity_groups g
                SET risk_level = CASE WHEN s.is_high THEN 'HIGH'
                                      WHEN s.is_med  THEN 'MEDIUM'
                                      ELSE 'LOW' END,
                    risk_signals = s.sig,
                    updated_at = NOW()
                FROM signals s
                WHERE s.group_id = g.group_id;
            """, {"high": HIGH_PATTERN, "med": MEDIUM_PATTERN})
            n = cur.rowcount
            conn.commit()
            _log(f"  updated {n:,} entity_groups")

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT risk_level, COUNT(*) FROM entity_groups
            GROUP BY risk_level ORDER BY risk_level;
        """)
        for lvl, n in cur.fetchall():
            print(f"    {lvl or '(null)':6s} : {n:,}")


# -------------------------------------------------------------------------- stats

def stats():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM entity_groups;")
        n_groups = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM entity_links;")
        n_links = cur.fetchone()[0]
        cur.execute("""
            SELECT risk_level, COUNT(*) FROM entity_groups
            GROUP BY risk_level ORDER BY risk_level NULLS LAST;
        """)
        risk_breakdown = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM entity_groups WHERE num_sources >= 5;")
        n_ge5 = cur.fetchone()[0]
        cur.execute("""
            SELECT canonical_name, num_sources, num_records, risk_level, source_list[1:6]
            FROM entity_groups ORDER BY num_sources DESC, num_records DESC LIMIT 20;
        """)
        top = cur.fetchall()
        cur.execute("""
            SELECT canonical_name, num_sources, risk_signals[1:5], source_list[1:5]
            FROM entity_groups
            WHERE risk_level='HIGH' ORDER BY num_sources DESC, num_records DESC LIMIT 15;
        """)
        top_high = cur.fetchall()

    print("\n===== KNOWLEDGE GRAPH STATS =====")
    print(f"  entity_groups: {n_groups:,}")
    print(f"  entity_links:  {n_links:,}")
    print(f"  Entities in 5+ sources: {n_ge5:,}")
    print(f"  Risk-level breakdown:")
    for lvl, n in risk_breakdown:
        print(f"    {lvl or '(null)':6s} : {n:,}")
    print(f"\n  Top 20 entities by source count:")
    print(f"    {'canonical_name':50s} {'sources':>7s} {'records':>7s} {'risk':6s}  source_list[:6]")
    for nm, ns, nr, risk, srcs in top:
        srcs_s = ",".join(srcs or [])[:70]
        print(f"    {(nm or '')[:48]:50s} {ns:7d} {nr:7d} {risk or '?':6s}  {srcs_s}")
    print(f"\n  Top 15 HIGH-risk entities:")
    for nm, ns, sigs, srcs in top_high:
        sig_s = ",".join((sigs or []))[:60]
        src_s = ",".join((srcs or []))[:60]
        print(f"    {(nm or '')[:48]:50s} {ns:4d} | signals: {sig_s} | srcs: {src_s}")


# ------------------------------------------------------------------------- search

def search(query: str, k: int = 20):
    with connect() as conn, conn.cursor() as cur:
        _log(f"search: {query!r}")
        # Tighten pg_trgm threshold for this session (not registered as a GUC
        # so ALTER SYSTEM can't make it permanent — set per-session here).
        try:
            cur.execute("SET pg_trgm.similarity_threshold = 0.5;")
        except Exception:
            pass
        # 1) Exact-via-group
        cur.execute("""
            SELECT group_id, canonical_name, num_sources, num_records, risk_level, source_list
            FROM entity_groups
            WHERE LOWER(TRIM(canonical_name)) = LOWER(TRIM(%s));
        """, (query,))
        grp = cur.fetchone()
        if grp:
            print("\n[exact group match]")
            print(f"  canonical: {grp[1]!r}")
            print(f"  group_id:  {grp[0]}")
            print(f"  sources={grp[2]}  records={grp[3]}  risk={grp[4]}")
            print(f"  source_list: {', '.join(grp[5] or [])}")
            # Linked records
            cur.execute("""
                SELECT record_id_a, name_a, source_a, record_id_b, name_b, source_b,
                       match_type, similarity_score
                FROM entity_links
                WHERE entity_group_id = %s
                ORDER BY source_b;
            """, (grp[0],))
            print(f"  links ({cur.rowcount}):")
            for r in cur.fetchall():
                print(f"    [{r[6]} sim={r[7]:.2f}] {r[2]:30s} {r[1][:30]:30s} <-> {r[5]:30s} {r[4][:50]}")
        else:
            print("\n[no exact group match]")

        # 2) Fuzzy similarity search in watchlist_records (uses pg_trgm GIN idx)
        cur.execute("""
            SELECT id, name, source_id, similarity(LOWER(name), LOWER(%s)) AS sim
            FROM watchlist_records
            WHERE name %% %s
            ORDER BY sim DESC
            LIMIT %s;
        """, (query, query, k))
        print(f"\n[fuzzy matches in watchlist_records] (top {k})")
        for rid, nm, src, sim in cur.fetchall():
            print(f"  {sim:.2f}  {src:30s}  {nm[:80]}  (id={rid})")


# ---------------------------------------------------------------------- entrypoint

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-exact", action="store_true")
    ap.add_argument("--build-fuzzy", action="store_true")
    ap.add_argument("--risk-score",  action="store_true")
    ap.add_argument("--stats",       action="store_true")
    ap.add_argument("--search",      type=str, help="search for entity by name")
    ap.add_argument("--reset", action="store_true", help="truncate before --build-exact")
    ap.add_argument("--limit", type=int, default=10_000, help="cap on --build-fuzzy candidates")
    ap.add_argument("--threshold", type=float, default=0.7, help="--build-fuzzy similarity cutoff")
    args = ap.parse_args()

    if not any([args.build_exact, args.build_fuzzy, args.risk_score, args.stats, args.search]):
        ap.print_help()
        sys.exit(0)

    if args.build_exact:
        build_exact(reset=args.reset)
    if args.risk_score:
        risk_score()
    if args.build_fuzzy:
        build_fuzzy(limit=args.limit, threshold=args.threshold)
    if args.stats:
        stats()
    if args.search:
        search(args.search)


if __name__ == "__main__":
    main()
