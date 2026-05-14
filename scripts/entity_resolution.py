"""
scripts/entity_resolution.py — cross-source entity resolution.

Joins every record in watchlist_records into clusters that represent
one real-world entity, using four match strategies in priority order:

  1. EXACT     — normalised names match byte-for-byte (conf 1.00)
  2. TOKEN     — sorted token bags match (conf 0.90)
  3. SUBSET    — one token bag contains the other (conf 0.70)
  4. TRIGRAM   — pg_trgm similarity >= 0.50 (conf = similarity)

To avoid O(N^2) we BLOCK by the first 3 characters of every name token
of length >= 3. Two records are compared only if they share at least
one such block, AND they come from DIFFERENT source_agency values.

Outputs:
  PG:  entity_clusters, entity_cluster_members tables.
  CSV: reports/entity_clusters.csv (cluster_id, canonical_name,
       record_count, agency_count, agencies, risk_score, match_types).

CLI:
  python scripts/entity_resolution.py --setup    # create tables
  python scripts/entity_resolution.py            # full pass
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict, Counter

try:
    import psycopg2
    from psycopg2.extras import execute_values
except Exception:
    psycopg2 = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
CSV_OUT = os.path.join(REPORTS_DIR, "entity_clusters.csv")

# Risk scoring weights per agency family. Keys are matched as a
# substring against source_agency (case-insensitive).
RISK_WEIGHTS = [
    (("cbi", "central bureau of investigation"), 30),
    (("nia", "national investigation agency"), 30),
    (("mha", "ministry of home affairs"), 30),
    (("unsc",), 30),
    (("interpol",), 30),
    (("sebi", "securities and exchange board"), 20),
    (("bse", "bombay stock exchange"), 20),
    (("nse", "national stock exchange"), 20),
    (("fiu", "financial intelligence unit"), 20),
    # Bank wilful-defaulter lists
    (("bank", "bom", "bob", "boi", "uco", "iob", "psb", "ib "), 15),
    # Everything else gets 10
]

CREATE_SQL = """
DROP TABLE IF EXISTS entity_cluster_members CASCADE;
DROP TABLE IF EXISTS entity_clusters CASCADE;
CREATE TABLE entity_clusters (
    cluster_id     SERIAL PRIMARY KEY,
    canonical_name VARCHAR(500),
    record_count   INTEGER,
    agency_count   INTEGER,
    agencies       TEXT[],
    risk_score     INTEGER,
    match_types    TEXT[],
    created_at     TIMESTAMP DEFAULT NOW(),
    updated_at     TIMESTAMP DEFAULT NOW()
);
CREATE INDEX entity_clusters_risk_idx ON entity_clusters (risk_score DESC);
CREATE INDEX entity_clusters_agcount_idx ON entity_clusters (agency_count DESC);

CREATE TABLE entity_cluster_members (
    id                  SERIAL PRIMARY KEY,
    cluster_id          INTEGER REFERENCES entity_clusters(cluster_id) ON DELETE CASCADE,
    watchlist_record_id INTEGER,
    source_agency       VARCHAR(200),
    source_list         VARCHAR(200),
    name_as_listed      VARCHAR(500),
    match_type          VARCHAR(20),
    confidence          FLOAT
);
CREATE INDEX entity_cluster_members_cluster_idx
    ON entity_cluster_members (cluster_id);
CREATE INDEX entity_cluster_members_record_idx
    ON entity_cluster_members (watchlist_record_id);
"""


# ---------- normalization --------------------------------------------------
_HONORIFICS = re.compile(
    r"\b(?:M/s\.?|Mr\.?|Mrs\.?|Ms\.?|Shri\.?|Smt\.?|Dr\.?|CA\.?|"
    r"Sri\.?|Prof\.?)\b", re.I)
_SUFFIXES = re.compile(
    r"\b(?:Ltd\.?|Limited|Pvt\.?|Private|Co\.?|Company|Corp\.?|Corporation|"
    r"LLP|LLC|Inc\.?|Industries|Enterprises?|Trust|Foundation|Society)\b",
    re.I)
_NON_WORD = re.compile(r"[^\w\s]+")
_WS = re.compile(r"\s+")
_STOP_TOKENS = {"the", "and", "&", "of", "for", "a", "an", "huf", "ors",
                "others", "anr"}


def normalize(name):
    if not name:
        return ""
    s = name.strip().lower()
    s = _HONORIFICS.sub(" ", s)
    s = _SUFFIXES.sub(" ", s)
    s = _NON_WORD.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


def tokens(norm):
    return tuple(sorted(t for t in norm.split() if t and t not in _STOP_TOKENS))


# ---------- DB helpers ----------------------------------------------------
def _db():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed")
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        user=os.environ.get("PG_USER", "aayush"),
        password=os.environ.get("PG_PASSWORD", "aayush123"),
        dbname=os.environ.get("PG_DB", "risk_pipeline"),
    )


def setup():
    conn = _db()
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
    conn.commit()
    conn.close()
    print("OK: entity_clusters + entity_cluster_members ready")


# ---------- union-find ----------------------------------------------------
class DSU:
    __slots__ = ("p", "rank")
    def __init__(self):
        self.p = {}
        self.rank = {}
    def add(self, x):
        if x not in self.p:
            self.p[x] = x
            self.rank[x] = 0
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return True


# ---------- core algorithm ------------------------------------------------
def risk_score(agencies):
    score = 0
    for ag in agencies:
        agl = (ag or "").lower()
        weight = 10
        for keys, w in RISK_WEIGHTS:
            if any(k in agl for k in keys):
                weight = w
                break
        score += weight
    return min(score, 100)


def run(min_trigram=0.5, verbose=True):
    t0 = time.time()
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, source_agency, source_list, name "
        "FROM watchlist_records WHERE name IS NOT NULL AND length(name) >= 2"
    )
    rows = cur.fetchall()
    print(f"  pulled {len(rows):,} records in {time.time()-t0:.1f}s")

    # ---- normalize & tokenize ----
    records = []  # list of (id, agency, list_, name, norm, tok)
    for rid, ag, lst, nm in rows:
        norm = normalize(nm)
        if len(norm) < 3:
            continue
        tok = tokens(norm)
        records.append((rid, ag, lst, nm, norm, tok))
    print(f"  normalized {len(records):,} rows ({time.time()-t0:.1f}s)")

    # ---- DSU on record id ----
    dsu = DSU()
    for r in records:
        dsu.add(r[0])
    match_type = {}   # rid_pair -> ("EXACT"|"TOKEN"|"SUBSET"|"TRIGRAM", conf)

    def mark_union(a, b, m_type, conf):
        if a == b:
            return
        if dsu.union(a, b):
            pass
        key = (min(a, b), max(a, b))
        if key not in match_type:
            match_type[key] = (m_type, conf)

    # ---- index by normalized name + token bag ----
    by_norm = defaultdict(list)
    by_tok = defaultdict(list)
    for r in records:
        by_norm[r[4]].append(r)
        if r[5]:
            by_tok[r[5]].append(r)

    # Exact normalized name match
    for norm, group in by_norm.items():
        if len(group) < 2:
            continue
        # union across different agencies only
        first = group[0]
        for other in group[1:]:
            if other[1] != first[1]:
                mark_union(first[0], other[0], "EXACT", 1.00)
            # but also union same-norm records inside the same agency so a
            # single cluster forms — even though same-source matches are
            # uninteresting, the cluster needs to contain all of them.
            mark_union(first[0], other[0], "EXACT", 1.00)
    print(f"  exact pass done ({time.time()-t0:.1f}s)")

    # Token-bag match (sorted token tuple identical, different norm)
    for tok, group in by_tok.items():
        if len(group) < 2:
            continue
        first = group[0]
        for other in group[1:]:
            mark_union(first[0], other[0], "TOKEN", 0.90)
    print(f"  token pass done ({time.time()-t0:.1f}s)")

    # ---- blocking index for subset + trigram ----
    # block on first 3 chars of any token of length >= 3
    blocks = defaultdict(list)
    for r in records:
        for t in r[5]:
            if len(t) >= 3:
                blocks[t[:3]].append(r)
    # cap absurdly large blocks (e.g., "the", "ltd") to avoid quadratic blow-up
    BLOCK_CAP = 800
    big_blocks = sum(1 for v in blocks.values() if len(v) > BLOCK_CAP)
    if big_blocks:
        print(f"  trimming {big_blocks} oversized blocks to {BLOCK_CAP}")
    for k in list(blocks):
        if len(blocks[k]) > BLOCK_CAP:
            blocks[k] = blocks[k][:BLOCK_CAP]
    print(f"  built {len(blocks)} blocks ({time.time()-t0:.1f}s)")

    # Subset pass: for each pair in same block, if one token set ⊆ other and
    # both sides have ≥2 tokens (avoid trivial single-token matches that the
    # trigram pass also catches).
    subset_pairs = 0
    seen = set()
    for k, group in blocks.items():
        if len(group) < 2:
            continue
        n = len(group)
        for i in range(n):
            a = group[i]; ta = set(a[5])
            if len(ta) < 2:
                continue
            for j in range(i + 1, n):
                b = group[j]; tb = set(b[5])
                if len(tb) < 2:
                    continue
                key = (min(a[0], b[0]), max(a[0], b[0]))
                if key in seen:
                    continue
                seen.add(key)
                # Skip if already merged elsewhere.
                if dsu.find(a[0]) == dsu.find(b[0]):
                    continue
                if ta <= tb or tb <= ta:
                    mark_union(a[0], b[0], "SUBSET", 0.70)
                    subset_pairs += 1
    print(f"  subset pass done ({time.time()-t0:.1f}s, {subset_pairs} pairs)")

    # ---- trigram pass — only candidates: pairs from same block whose pair
    # is not already merged. We compute similarity inside Python via a
    # quick char-trigram Jaccard rather than hitting Postgres per-pair.
    def trigrams(s):
        s = f"  {s} "
        return {s[i:i+3] for i in range(len(s) - 2)}

    tri_cache = {}
    def tri(r):
        if r[0] not in tri_cache:
            tri_cache[r[0]] = trigrams(r[4])
        return tri_cache[r[0]]

    trigram_pairs = 0
    seen = set()
    for k, group in blocks.items():
        n = len(group)
        if n < 2 or n > 400:   # extra guard
            continue
        for i in range(n):
            a = group[i]
            ta = tri(a)
            if not ta:
                continue
            for j in range(i + 1, n):
                b = group[j]
                # cross-agency only for trigram (it's the weakest)
                if a[1] == b[1]:
                    continue
                key = (min(a[0], b[0]), max(a[0], b[0]))
                if key in seen:
                    continue
                seen.add(key)
                if dsu.find(a[0]) == dsu.find(b[0]):
                    continue
                tb = tri(b)
                inter = len(ta & tb)
                if not inter:
                    continue
                sim = inter / (len(ta) + len(tb) - inter)
                if sim >= min_trigram:
                    mark_union(a[0], b[0], "TRIGRAM", round(sim, 3))
                    trigram_pairs += 1
    print(f"  trigram pass done ({time.time()-t0:.1f}s, {trigram_pairs} pairs)")

    # ---- materialise clusters ----
    cluster_members = defaultdict(list)
    for r in records:
        cluster_members[dsu.find(r[0])].append(r)

    # Only keep clusters with at least one cross-agency relationship —
    # singleton clusters and same-agency-only clusters are uninteresting.
    final_clusters = []
    cluster_match_types = {}
    for root, members in cluster_members.items():
        agencies = sorted({m[1] for m in members})
        if len(agencies) < 2:
            continue
        # collect match types that bound this cluster
        mtypes = set()
        member_ids = {m[0] for m in members}
        for (a, b), (mt, _conf) in match_type.items():
            if a in member_ids and b in member_ids:
                mtypes.add(mt)
        cluster_match_types[root] = mtypes
        # canonical name = longest name in the cluster (most informative)
        canonical = max(members, key=lambda m: len(m[3] or ""))[3]
        final_clusters.append((root, canonical, members, agencies, mtypes))
    print(f"  {len(final_clusters):,} cross-agency clusters "
          f"({time.time()-t0:.1f}s)")

    # ---- write to PG ----
    cur.execute("DELETE FROM entity_cluster_members")
    cur.execute("DELETE FROM entity_clusters")
    cluster_rows = []
    member_rows = []
    for root, canonical, members, agencies, mtypes in final_clusters:
        score = risk_score(agencies)
        cluster_rows.append((canonical[:500], len(members), len(agencies),
                              agencies, score, sorted(mtypes)))
    if cluster_rows:
        # execute_values returns the result list directly when fetch=True.
        rows_back = execute_values(cur,
            "INSERT INTO entity_clusters "
            "(canonical_name, record_count, agency_count, agencies, risk_score, match_types) "
            "VALUES %s RETURNING cluster_id",
            cluster_rows, fetch=True, page_size=1000)
        if len(rows_back) != len(final_clusters):
            raise RuntimeError(
                f"insert mismatch: got {len(rows_back)} ids for "
                f"{len(final_clusters)} clusters"
            )
        cluster_id_by_root = {fc[0]: row[0] for fc, row in
                              zip(final_clusters, rows_back)}
        for root, canonical, members, _ag, mtypes in final_clusters:
            cid = cluster_id_by_root[root]
            for m in members:
                # match_type & confidence per member: pick the best one
                # this member participated in
                best_mt, best_conf = "INFERRED", 0.5
                for (a, b), (mt, conf) in match_type.items():
                    if m[0] not in (a, b):
                        continue
                    rank = {"EXACT": 4, "TOKEN": 3, "SUBSET": 2, "TRIGRAM": 1}.get(mt, 0)
                    cur_rank = {"EXACT": 4, "TOKEN": 3, "SUBSET": 2, "TRIGRAM": 1,
                                 "INFERRED": 0}.get(best_mt, 0)
                    if rank > cur_rank:
                        best_mt, best_conf = mt, conf
                member_rows.append((cid, m[0], m[1], m[2], (m[3] or "")[:500],
                                     best_mt, best_conf))
        # batched insert of members (~150K rows possible)
        for i in range(0, len(member_rows), 5000):
            execute_values(cur,
                "INSERT INTO entity_cluster_members "
                "(cluster_id, watchlist_record_id, source_agency, source_list, "
                " name_as_listed, match_type, confidence) VALUES %s",
                member_rows[i:i + 5000])
    conn.commit()
    print(f"  persisted {len(cluster_rows):,} clusters and "
          f"{len(member_rows):,} members ({time.time()-t0:.1f}s)")

    # ---- CSV export ----
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(CSV_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cluster_id", "canonical_name", "record_count",
                    "agency_count", "agencies", "risk_score", "match_types"])
        cur.execute(
            "SELECT cluster_id, canonical_name, record_count, agency_count, "
            "       agencies, risk_score, match_types "
            "FROM entity_clusters "
            "ORDER BY risk_score DESC, agency_count DESC, record_count DESC"
        )
        for r in cur.fetchall():
            (cid, cname, n, na, ags, rs, mts) = r
            w.writerow([cid, cname, n, na, "; ".join(ags or []), rs,
                         "; ".join(mts or [])])
    print(f"  wrote {CSV_OUT}")

    # ---- distribution summary ----
    cur.execute(
        "SELECT agency_count, count(*) FROM entity_clusters "
        "GROUP BY agency_count ORDER BY agency_count"
    )
    dist = list(cur.fetchall())
    cur.execute(
        "SELECT cluster_id, canonical_name, agencies, risk_score "
        "FROM entity_clusters ORDER BY risk_score DESC, agency_count DESC LIMIT 20"
    )
    top = cur.fetchall()
    conn.close()

    print("\n=== Cross-agency entity distribution ===")
    print(f"  Total clusters (≥2 agencies): {sum(c for _, c in dist):,}")
    for ac, c in dist:
        if ac <= 6:
            print(f"    {ac} agencies: {c:,}")
        else:
            print(f"    {ac}+ agencies: {c:,}")
    print("\n=== Top 20 highest-risk cross-agency clusters ===")
    for cid, cn, ags, rs in top:
        print(f"  [#{cid:>5}] score={rs:>3}  "
              f"{(cn or '')[:60]:<60}  "
              f"agencies={', '.join(ags or [])[:120]}")
    print(f"\nTotal time: {time.time()-t0:.1f}s")
    return len(cluster_rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--min-trigram", type=float, default=0.55,
                    help="trigram-similarity threshold (default 0.55)")
    args = ap.parse_args()
    if args.setup:
        setup()
        return
    setup()  # idempotent — recreate tables before each full run
    run(min_trigram=args.min_trigram)


if __name__ == "__main__":
    main()
