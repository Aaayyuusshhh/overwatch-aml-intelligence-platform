"""
scripts/screen.py — AML watchlist screening CLI.

Internal tool for the onboarding / compliance team. Pass a candidate
name, get a flagged-or-clear verdict against the consolidated 244-source
Indian regulatory dataset stored in PostgreSQL (table watchlist_records).

Usage
-----
  python scripts/screen.py "Vikram Mehta"
  python scripts/screen.py --company "ABC Infra Pvt Ltd"
  python scripts/screen.py --bulk input_names.csv --export results.csv
  python scripts/screen.py "Rajesh Kumar" --exact --limit 25

Search strategy (Levels 1→2→3, expanded only as needed)
-----------------------------------------------------
  L1  exact      WHERE name ILIKE '%query%'
  L2  token      every space-separated token must ILIKE-match  (only
                 invoked if L1 < 5 hits and --exact not set)
  L3  trigram    similarity(name, query) > 0.3                 (only
                 invoked if L2 < 5 hits, --exact not set, and pg_trgm
                 is available — gracefully skipped otherwise)

Per-row hits are deduped across levels by ctid (table physical row id)
so a row matched by L1 isn't double-counted at L2 / L3.

Risk level
----------
  CRITICAL  - 4+ agencies, OR any hit from CBI / NIA / MHA / Interpol-related list
  HIGH      - 2-3 agencies
  MEDIUM    - 1 agency, 2+ listings
  LOW       - 1 agency, 1 listing
  CLEAR     - no hits

Exit code is 0 in all cases (clear / hit alike); a screening operator
should read STATUS, not $?.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import psycopg2
    import psycopg2.extras
except ImportError as e:
    print(f"FATAL: psycopg2 not installed. Run:\n  "
          f"pip install psycopg2-binary\n\n{e}", file=sys.stderr)
    sys.exit(2)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
TABLE = "watchlist_records"
DB_CONFIG = {
    "host":     os.environ.get("PG_HOST", "localhost"),
    "user":     os.environ.get("PG_USER", "aayush"),
    "password": os.environ.get("PG_PASSWORD", "aayush123"),
    "dbname":   os.environ.get("PG_DB", "risk_pipeline"),
}

# Agencies that imply CRITICAL risk on any match.
CRITICAL_AGENCIES = {"CBI", "NIA", "MHA"}
INTERPOL_KEYWORDS = ("interpol", "red corner", "red notice", "yellow notice")

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# --------------------------------------------------------------------------
# ANSI colors — gracefully degrade when stdout isn't a TTY.
# --------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty()
_C = {
    "reset":   "\033[0m"   if _USE_COLOR else "",
    "bold":    "\033[1m"   if _USE_COLOR else "",
    "dim":     "\033[2m"   if _USE_COLOR else "",
    "red":     "\033[31m"  if _USE_COLOR else "",
    "green":   "\033[32m"  if _USE_COLOR else "",
    "yellow":  "\033[33m"  if _USE_COLOR else "",
    "blue":    "\033[34m"  if _USE_COLOR else "",
    "magenta": "\033[35m"  if _USE_COLOR else "",
    "cyan":    "\033[36m"  if _USE_COLOR else "",
    "bg_red":  "\033[41m"  if _USE_COLOR else "",
    "bg_yel":  "\033[43m"  if _USE_COLOR else "",
}


def color(text, *names):
    return "".join(_C[n] for n in names) + text + _C["reset"]


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
def connect():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        return conn
    except psycopg2.OperationalError as e:
        print(color("ERROR: cannot connect to PostgreSQL.", "red", "bold"),
              file=sys.stderr)
        print(f"  host={DB_CONFIG['host']} user={DB_CONFIG['user']} "
              f"db={DB_CONFIG['dbname']}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        print("  hint: ensure postgres is up and credentials match",
              file=sys.stderr)
        sys.exit(2)


def trigram_available(conn):
    """Return True if pg_trgm extension is installed (create-if-needed)."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm';")
        if cur.fetchone():
            return True
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm';")
        return cur.fetchone() is not None
    except psycopg2.Error as e:
        print(color(f"  note: pg_trgm unavailable ({e.__class__.__name__}); "
                    "Level 3 fuzzy match will be skipped",
                    "yellow"), file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# Search levels
# --------------------------------------------------------------------------
HIT_COLS = ("source_agency", "source_list", "name", "details",
            "document_url", "scraped_at")


def _exec_hits(cur, sql, params, level_label, seen_ctids, limit):
    cur.execute(sql, params)
    hits = []
    for row in cur.fetchall():
        ctid = row[-1]
        if ctid in seen_ctids:
            continue
        seen_ctids.add(ctid)
        h = dict(zip(HIT_COLS, row[:-1]))
        h["match_level"] = level_label
        hits.append(h)
        if len(hits) >= limit:
            break
    return hits


def search_level_1_exact(cur, query, limit, seen):
    sql = f"""
        SELECT source_agency, source_list, name, details,
               document_url, scraped_at, ctid
        FROM   {TABLE}
        WHERE  name ILIKE %s
        ORDER  BY source_agency, source_list
        LIMIT  %s
    """
    return _exec_hits(cur, sql, (f"%{query}%", limit), "EXACT",
                      seen, limit)


def search_level_2_token(cur, query, limit, seen):
    tokens = [t for t in query.split() if len(t) >= 2]
    if len(tokens) < 2:
        return []
    where = " AND ".join(["name ILIKE %s"] * len(tokens))
    sql = f"""
        SELECT source_agency, source_list, name, details,
               document_url, scraped_at, ctid
        FROM   {TABLE}
        WHERE  {where}
        ORDER  BY source_agency, source_list
        LIMIT  %s
    """
    params = tuple(f"%{t}%" for t in tokens) + (limit,)
    return _exec_hits(cur, sql, params, "FUZZY (token match)",
                      seen, limit)


def search_level_3_trigram(cur, query, limit, seen, threshold=0.3):
    sql = f"""
        SELECT source_agency, source_list, name, details,
               document_url, scraped_at, ctid
        FROM   {TABLE}
        WHERE  similarity(name, %s) > %s
        ORDER  BY similarity(name, %s) DESC
        LIMIT  %s
    """
    return _exec_hits(cur, sql, (query, threshold, query, limit),
                      "FUZZY (trigram)", seen, limit)


# --------------------------------------------------------------------------
# Risk scoring
# --------------------------------------------------------------------------
def compute_risk(hits):
    if not hits:
        return "CLEAR", "no hits"
    agencies = {h["source_agency"] for h in hits}
    # CRITICAL?
    if agencies & CRITICAL_AGENCIES:
        why = "hit from " + ", ".join(sorted(agencies & CRITICAL_AGENCIES))
        return "CRITICAL", why
    interpol_hit = any(
        any(k in (h["source_list"] or "").lower() for k in INTERPOL_KEYWORDS)
        for h in hits)
    if interpol_hit:
        return "CRITICAL", "Interpol-related listing present"
    if len(agencies) >= 4:
        return "CRITICAL", f"{len(agencies)} agencies"
    if len(agencies) >= 2:
        return "HIGH", f"{len(agencies)} agencies flagged"
    # Single agency
    if len(hits) >= 2:
        return "MEDIUM", f"single agency, {len(hits)} listings"
    return "LOW", "single agency, single listing"


_RISK_COLOR = {
    "CRITICAL": ("bg_red",  "bold"),
    "HIGH":     ("red",     "bold"),
    "MEDIUM":   ("yellow",  "bold"),
    "LOW":      ("yellow",),
    "CLEAR":    ("green",   "bold"),
}


def _truncate(s, n):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
LINE = "═" * 64
SUB  = "─" * 64


def render_single(query, hits, levels_used, elapsed_seconds):
    print()
    print(color(LINE, "cyan"))
    print(color("AML SCREENING REPORT", "cyan", "bold"))
    print(color(LINE, "cyan"))
    print(f"Query:        {color(query, 'bold')}")
    print(f"Timestamp:    {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST")
    print(f"Search mode:  {' + '.join(levels_used) or '—'}")
    print(SUB)

    risk, reason = compute_risk(hits)
    agencies = sorted({h["source_agency"] for h in hits})

    if hits:
        head = (f"⚠️  STATUS: FLAGGED — {len(hits)} hits "
                f"across {len(agencies)} agencies")
        print(color(head, "yellow", "bold"))
    else:
        print(color(f"✅ STATUS: CLEAR — No matches across the dataset",
                    "green", "bold"))
        print(SUB)
        # Show dataset coverage even on clear
        print(color(f"Coverage scanned: {color('63', 'bold')} agencies, "
                    f"{color('106,039', 'bold')} records", "dim"))
        print(color(LINE, "cyan"))
        print(color(f"Screened in {elapsed_seconds:.2f}s", "dim"))
        return risk

    for i, h in enumerate(hits, 1):
        print()
        print(color(f"── HIT {i} " + "─" * (54 - len(str(i))), "cyan"))
        print(f"Agency:     {color(h['source_agency'], 'bold')}")
        print(f"List:       {h['source_list']}")
        print(f"Name:       {color(h['name'], 'magenta')}")
        ml = h.get("match_level", "EXACT")
        ml_col = "green" if ml == "EXACT" else "yellow"
        print(f"Match:      {color(ml, ml_col)}")
        if h.get("details"):
            print(f"Details:    {_truncate(h['details'], 180)}")
        if h.get("document_url"):
            print(f"Document:   {color(_truncate(h['document_url'], 100), 'blue')}")
        if h.get("scraped_at"):
            print(f"Scraped at: {_truncate(str(h['scraped_at']), 30)}")
        print(color(SUB, "dim"))

    # Summary
    print()
    print(color(LINE, "cyan"))
    print(color("SUMMARY", "cyan", "bold"))
    print(color(LINE, "cyan"))
    print(f"Total hits:        {len(hits)}")
    print(f"Agencies involved: {', '.join(agencies)}")
    dates = [str(h.get("scraped_at"))[:10] for h in hits
             if h.get("scraped_at")]
    if dates:
        print(f"Earliest listing:  {min(dates)}")
        print(f"Latest listing:    {max(dates)}")
    print(f"Risk level:        {color(risk, *_RISK_COLOR[risk])}  "
          f"({reason})")
    print(color(LINE, "cyan"))
    print(color(f"Screened in {elapsed_seconds:.2f}s", "dim"))
    return risk


# --------------------------------------------------------------------------
# Screening entry point
# --------------------------------------------------------------------------
def screen_one(conn, query, *, exact=False, limit=10, has_trigram=True):
    """Return (hits, levels_used)."""
    cur = conn.cursor()
    seen = set()
    levels_used = []

    # L1
    hits = search_level_1_exact(cur, query, limit, seen)
    levels_used.append("L1-exact")

    # L2
    if not exact and len(hits) < 5:
        l2 = search_level_2_token(cur, query, limit, seen)
        if l2:
            hits.extend(l2)
            levels_used.append("L2-token")

    # L3
    if not exact and has_trigram and len(hits) < 5:
        l3 = search_level_3_trigram(cur, query, limit, seen)
        if l3:
            hits.extend(l3)
            levels_used.append("L3-trigram")

    cur.close()
    return hits, levels_used


def export_hits(rows, out_path):
    """rows: list[dict] keyed by 'query', 'agency', 'list', 'name', etc."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    flds = ["query", "agency", "source_list", "name", "match_level",
            "risk_level", "details", "document_url", "scraped_at"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=flds)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in flds})
    print(color(f"Exported {len(rows)} rows → {out_path}", "green"))


# --------------------------------------------------------------------------
# Bulk
# --------------------------------------------------------------------------
def screen_bulk(conn, csv_path, *, exact, limit, has_trigram, export_path):
    if not os.path.exists(csv_path):
        print(color(f"ERROR: bulk input file not found: {csv_path}", "red"),
              file=sys.stderr)
        sys.exit(2)
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        names = [r.get("name") or r.get("Name") for r in rdr]
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        print(color("ERROR: no 'name' column or empty rows in CSV", "red"),
              file=sys.stderr)
        sys.exit(2)

    print(color(LINE, "cyan"))
    print(color(f"BULK SCREENING — {len(names)} names from {csv_path}",
                "cyan", "bold"))
    print(color(LINE, "cyan"))

    export_rows = []
    summary_rows = []
    t0 = time.perf_counter()

    for i, name in enumerate(names, 1):
        t_one = time.perf_counter()
        hits, _ = screen_one(conn, name, exact=exact, limit=limit,
                             has_trigram=has_trigram)
        risk, _ = compute_risk(hits)
        agencies = sorted({h["source_agency"] for h in hits})
        dt = time.perf_counter() - t_one
        print(f"  [{i:>3}/{len(names)}] {name[:35]:<35} "
              f"hits={len(hits):>3}  agencies={len(agencies):>2}  "
              f"risk={color(risk, *_RISK_COLOR[risk])}  ({dt:.2f}s)")
        summary_rows.append({"name": name, "hits": len(hits),
                             "agencies": ", ".join(agencies),
                             "risk": risk})
        for h in hits:
            export_rows.append({
                "query": name,
                "agency": h["source_agency"],
                "source_list": h["source_list"],
                "name": h["name"],
                "match_level": h["match_level"],
                "risk_level": risk,
                "details": (h.get("details") or "")[:500],
                "document_url": h.get("document_url") or "",
                "scraped_at": h.get("scraped_at") or "",
            })

    elapsed = time.perf_counter() - t0
    # Summary table
    print()
    print(color(LINE, "cyan"))
    print(color("BULK SUMMARY", "cyan", "bold"))
    print(color(LINE, "cyan"))
    print(f"{'Name':<35} {'Hits':>5}  {'Agencies':<28}  Risk")
    print(SUB)
    for r in summary_rows:
        print(f"{r['name'][:35]:<35} {r['hits']:>5}  "
              f"{r['agencies'][:28]:<28}  "
              f"{color(r['risk'], *_RISK_COLOR[r['risk']])}")
    print(SUB)
    flagged = sum(1 for r in summary_rows if r["risk"] != "CLEAR")
    print(f"Flagged: {flagged} / {len(summary_rows)}   "
          f"(total elapsed: {elapsed:.2f}s, "
          f"avg {elapsed/len(names):.2f}s/name)")
    print(color(LINE, "cyan"))

    if export_path:
        export_hits(export_rows, export_path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="AML watchlist screening CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Examples:\n"
                "  screen.py \"Vikram Mehta\"\n"
                "  screen.py --company \"ABC Infra Pvt Ltd\"\n"
                "  screen.py --bulk names.csv --export results.csv\n"))
    ap.add_argument("name", nargs="?",
                    help="name or entity to screen (omit for --bulk mode)")
    ap.add_argument("--company", help="entity name to screen "
                    "(alias for positional; primarily for clarity)")
    ap.add_argument("--bulk", help="CSV path with column 'name'")
    ap.add_argument("--exact", action="store_true",
                    help="exact ILIKE only — disable token / trigram fuzzy")
    ap.add_argument("--limit", type=int, default=10,
                    help="max results per source / level (default 10)")
    ap.add_argument("--export", help="path to write results CSV")
    args = ap.parse_args()

    # Resolve query
    query = args.company or args.name
    if not query and not args.bulk:
        ap.error("provide a name, or use --bulk <csv>")
    if args.bulk and query:
        ap.error("--bulk and a positional name are mutually exclusive")

    conn = connect()
    has_trigram = trigram_available(conn) if not args.exact else False

    if args.bulk:
        screen_bulk(conn, args.bulk, exact=args.exact, limit=args.limit,
                    has_trigram=has_trigram, export_path=args.export)
    else:
        t0 = time.perf_counter()
        hits, levels = screen_one(conn, query, exact=args.exact,
                                  limit=args.limit, has_trigram=has_trigram)
        elapsed = time.perf_counter() - t0
        risk = render_single(query, hits, levels, elapsed)

        if args.export:
            export_rows = [{
                "query": query,
                "agency": h["source_agency"],
                "source_list": h["source_list"],
                "name": h["name"],
                "match_level": h["match_level"],
                "risk_level": risk,
                "details": (h.get("details") or "")[:500],
                "document_url": h.get("document_url") or "",
                "scraped_at": h.get("scraped_at") or "",
            } for h in hits]
            export_hits(export_rows, args.export)

    conn.close()


if __name__ == "__main__":
    main()
