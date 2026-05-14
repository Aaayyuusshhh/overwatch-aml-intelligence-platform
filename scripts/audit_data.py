"""
audit_data.py - Apply quality checks to generic-engine output, flag
garbage, and clean up.

Per the engineer's quality-pass spec. Checks (only for html_generic
and html_block sources - custom-scraper output is trusted):

  Check 1 - garbage_nav   : >30% of 'name' values are nav terms
                            (Home, About, Contact, Login, ...)
  Check 2 - garbage_dupes : >50% of rows are exact duplicates
  Check 3 - garbage_empty : >70% of rows have empty name AND empty
                            details

Plus a SEBI-only cross-source check:
  garbage_duplicate_block : 3+ SEBI sources (ppt 108-119) using
                            html_block share identical row count and
                            identical 'name' value set - they
                            extracted the same nav/sidebar element.

Cleanup for any flagged source:
  - delete data/<source_id>.csv
  - DELETE FROM watchlist_records WHERE source_id = ...
  - sources.json: status -> 'failed', append auto-flag note

Usage:
    python scripts/audit_data.py            # apply cleanups
    python scripts/audit_data.py --dry-run  # report only
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict

import psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")

DB_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", 5432)),
    "dbname": os.environ.get("PGDATABASE", "risk_pipeline"),
    "user": os.environ.get("PGUSER", "aayush"),
    "password": os.environ.get("PGPASSWORD", "aayush123"),
}

NAV_TERMS = {
    "home", "about", "contact", "login", "search", "menu",
    "back", "next", "previous", "submit", "click here",
    "read more", "download", "skip to content",
}
SEBI_PPT_RANGE = range(108, 120)   # 108..119 inclusive
NAV_THRESHOLD = 0.30
DUP_THRESHOLD = 0.50
EMPTY_THRESHOLD = 0.70


def load_sources():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def csv_link_kind(rows):
    if not rows:
        return ""
    return (rows[0].get("link_kind") or "").strip()


def fraction_nav(rows):
    if not rows:
        return 0.0
    nav = 0
    for r in rows:
        name = (r.get("name") or "").strip().lower()
        if name in NAV_TERMS:
            nav += 1
    return nav / len(rows)


def fraction_dupes(rows):
    if not rows:
        return 0.0
    keys = [tuple(sorted(r.items())) for r in rows]
    counter = Counter(keys)
    dup_rows = sum(c for c in counter.values() if c >= 2)
    return dup_rows / len(rows)


def fraction_empty(rows):
    if not rows:
        return 1.0
    empty = 0
    for r in rows:
        n = (r.get("name") or "").strip()
        d = (r.get("details") or "").strip()
        if not n and not d:
            empty += 1
    return empty / len(rows)


def per_source_flag(rows):
    """Return (flag, sample_name). flag in {'clean', 'garbage_nav',
    'garbage_dupes', 'garbage_empty'}."""
    f_nav = fraction_nav(rows)
    f_dup = fraction_dupes(rows)
    f_emp = fraction_empty(rows)
    sample_name = next(((r.get("name") or "").strip() for r in rows
                        if (r.get("name") or "").strip()), "")
    if f_nav > NAV_THRESHOLD:
        return "garbage_nav", sample_name, (f_nav, f_dup, f_emp)
    if f_dup > DUP_THRESHOLD:
        return "garbage_dupes", sample_name, (f_nav, f_dup, f_emp)
    if f_emp > EMPTY_THRESHOLD:
        return "garbage_empty", sample_name, (f_nav, f_dup, f_emp)
    return "clean", sample_name, (f_nav, f_dup, f_emp)


def detect_sebi_duplicate_blocks(eligible):
    """eligible: dict of source_id -> rows (for SEBI html_block sources)
    Returns set of source_ids whose (row_count, frozenset_of_names)
    signature matches at least 2 other SEBI sources in the same group."""
    sigs = {}
    for sid, rows in eligible.items():
        names = frozenset((r.get("name") or "").strip() for r in rows)
        sigs[sid] = (len(rows), names)
    groups = defaultdict(list)
    for sid, sig in sigs.items():
        groups[sig].append(sid)
    flagged = set()
    for sig, sids in groups.items():
        if len(sids) >= 3:
            flagged.update(sids)
    return flagged


def cleanup(sid, reason, conn, sources_data, dry_run):
    csv_path = os.path.join(DATA_DIR, f"{sid}.csv")
    csv_deleted = False
    db_deleted = 0

    if not dry_run:
        if os.path.exists(csv_path):
            os.remove(csv_path)
            csv_deleted = True
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist_records WHERE source_id = %s", (sid,))
        db_deleted = cur.rowcount
        cur.close()
        for e in sources_data["sources"]:
            if e["id"] == sid:
                e["status"] = "failed"
                tag = f"auto-flagged: {reason}"
                existing = (e.get("notes") or "").strip()
                e["notes"] = f"{existing} | {tag}" if existing else tag
                break
    else:
        csv_deleted = os.path.exists(csv_path)
    return csv_deleted, db_deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print decisions, don't modify files or DB")
    args = ap.parse_args()

    sources_data = load_sources()
    sources = sources_data["sources"]
    by_id = {s["id"]: s for s in sources}

    # Build the audit set: type in (html, pdf), no custom scraper, CSV exists.
    audit_targets = []
    for s in sources:
        if s["type"] not in ("html", "pdf") or s.get("scraper"):
            continue
        path = os.path.join(DATA_DIR, f"{s['id']}.csv")
        if not os.path.exists(path):
            continue
        try:
            rows = load_csv(path)
        except Exception as e:
            print(f"  read_error {s['id']}: {e}")
            continue
        audit_targets.append((s, rows))

    print(f"Auditing {len(audit_targets)} sources (html/pdf, no custom scraper, CSV present)")
    print()

    # Per-source flagging.
    flag_for = {}    # sid -> flag string ('clean', 'garbage_*')
    sample_for = {}
    metrics_for = {}
    print("=" * 96)
    print(f"{'source_id':45s} {'rows':>5} {'link_kind':14s} {'flag':22s} {'sample_name':30s}")
    print("=" * 96)
    for s, rows in audit_targets:
        sid = s["id"]
        flag, sample, metrics = per_source_flag(rows)
        flag_for[sid] = flag
        sample_for[sid] = sample
        metrics_for[sid] = metrics
        link_kind = csv_link_kind(rows)
        sn = (sample or "")[:28]
        print(f"{sid[:44]:45s} {len(rows):>5d} {link_kind:14s} {flag:22s} {sn}")

    # SEBI cross-source check (only html_block, only ppt 108-119).
    sebi_block_eligible = {}
    for s, rows in audit_targets:
        if s.get("ppt_number") in SEBI_PPT_RANGE and csv_link_kind(rows) == "html_block":
            sebi_block_eligible[s["id"]] = rows
    sebi_flagged = detect_sebi_duplicate_blocks(sebi_block_eligible)

    print()
    print(f"SEBI html_block candidates: {len(sebi_block_eligible)}")
    print(f"SEBI cross-source duplicates flagged: {len(sebi_flagged)}")
    for sid in sorted(sebi_flagged):
        if flag_for.get(sid) == "clean":
            flag_for[sid] = "garbage_duplicate_block"

    # Apply cleanups.
    print()
    if args.dry_run:
        print("DRY RUN - no files or DB rows modified")
    conn = None if args.dry_run else psycopg2.connect(**DB_CONFIG)
    deleted_csvs = 0
    deleted_db_rows = 0
    cleanup_summary = []
    try:
        for sid, flag in flag_for.items():
            if flag == "clean":
                continue
            csv_d, db_d = cleanup(sid, flag, conn, sources_data, args.dry_run)
            deleted_csvs += int(csv_d)
            deleted_db_rows += db_d
            cleanup_summary.append((sid, flag, csv_d, db_d))
        if not args.dry_run and conn is not None:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()

    if not args.dry_run:
        with open(SOURCES_PATH, "w", encoding="utf-8") as f:
            json.dump(sources_data, f, indent=2, ensure_ascii=False)

    # Summaries.
    flag_counts = Counter(flag_for.values())
    print()
    print("=" * 60)
    print("FLAG TOTALS")
    print("=" * 60)
    for k in sorted(flag_counts, key=lambda x: -flag_counts[x]):
        print(f"  {k:25s}  {flag_counts[k]:4d}")

    print()
    print("=" * 60)
    print("CLEANUP")
    print("=" * 60)
    print(f"  CSV files deleted   : {deleted_csvs}")
    print(f"  DB rows deleted     : {deleted_db_rows}")
    print(f"  Sources reclassified: {sum(1 for _,_,_,_ in cleanup_summary)}")
    print()
    if cleanup_summary:
        print("Reclassified to 'failed':")
        for sid, flag, csv_d, db_d in cleanup_summary:
            print(f"  {sid:50s} flag={flag:25s} csv={int(bool(csv_d))} db_rows={db_d}")


if __name__ == "__main__":
    main()
