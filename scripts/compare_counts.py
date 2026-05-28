"""
Compare per-source watchlist_records counts before vs after a scrape run.

Reads pre-scrape counts from logs/pre_scrape_counts.json (written by
run_all.sh at the top of the run) and the FATF list from data/fatf_lists.csv
(to detect new black/grey countries), then queries current per-source
counts. Writes a structured diff to logs/post_scrape_diff.json so
send_daily_report.py can surface concrete changes (e.g. "OpenSanctions
PEPs: +85") instead of just a total delta.

Sections in the output JSON:
  generated_at            ISO timestamp
  pre_total / post_total  totals across all sources (for sanity)
  delta_total
  added[]                 sources that gained rows
  removed[]               sources that lost rows
  zeroed[]                sources that went from >0 to 0 (likely broken)
  new_sources[]           sources present today but not in the pre snapshot
  failed_scrapers[]       parsed from logs/run_YYYY-MM-DD.log "WARN:" lines
  fatf_changes            {added_countries[], removed_countries[]}
  summary                 short human-readable bullets

Exit code is always 0 — this script is diagnostic, never blocks the pipeline.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys

import psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
PRE_PATH = os.path.join(LOG_DIR, "pre_scrape_counts.json")
DIFF_PATH = os.path.join(LOG_DIR, "post_scrape_diff.json")
FATF_PREV_PATH = os.path.join(LOG_DIR, "fatf_previous.json")
FATF_CSV = os.path.join(PROJECT_ROOT, "data", "fatf_lists.csv")

DB = dict(
    host=os.environ.get("PG_HOST", "localhost"),
    user=os.environ.get("PG_USER", "aayush"),
    password=os.environ.get("PG_PASSWORD", "aayush123"),
    dbname=os.environ.get("PG_DB", "risk_pipeline"),
)


def _load_pre_counts() -> dict:
    if not os.path.exists(PRE_PATH):
        return {}
    try:
        with open(PRE_PATH) as f:
            return {k: int(v) for k, v in json.load(f).items()}
    except Exception as e:
        print(f"WARN: could not read {PRE_PATH}: {e}", file=sys.stderr)
        return {}


def _current_counts() -> dict:
    """Live per-source row counts. Returns {} on DB error."""
    try:
        with psycopg2.connect(connect_timeout=15, **DB) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source_id, COUNT(*) "
                "FROM watchlist_records "
                "WHERE source_id IS NOT NULL AND source_id <> '' "
                "GROUP BY source_id"
            )
            return {sid: int(n) for sid, n in cur.fetchall()}
    except Exception as e:
        print(f"WARN: could not query current counts: {e}", file=sys.stderr)
        return {}


def _today_run_log() -> str | None:
    """Path to today's run log, if it exists."""
    path = os.path.join(LOG_DIR, f"run_{dt.date.today().isoformat()}.log")
    return path if os.path.exists(path) else None


def _parse_failed_scrapers(log_path: str | None) -> list[str]:
    """Grep today's run log for WARN/ERROR-flagged scrapers. Best-effort."""
    if not log_path:
        return []
    failures: list[str] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r"WARN: ([^\n]+?)(?: timeout/error|\sfailed|\stimeout|$)", line)
                if m:
                    fail = m.group(1).strip()
                    if fail and fail not in failures:
                        failures.append(fail)
    except Exception as e:
        print(f"WARN: could not parse run log: {e}", file=sys.stderr)
    return failures[:20]


def _fatf_today() -> dict:
    """Parse data/fatf_lists.csv into {'black': [...], 'grey': [...]}."""
    out = {"black": [], "grey": []}
    if not os.path.exists(FATF_CSV):
        return out
    try:
        with open(FATF_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lst = (row.get("source_list") or "").lower()
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                if "black" in lst:
                    out["black"].append(name)
                elif "grey" in lst or "monitoring" in lst:
                    out["grey"].append(name)
    except Exception as e:
        print(f"WARN: could not parse FATF CSV: {e}", file=sys.stderr)
    return out


def _fatf_changes(today: dict) -> dict:
    """Diff today's FATF lists against the previous run, persist today's."""
    prev = {"black": [], "grey": []}
    if os.path.exists(FATF_PREV_PATH):
        try:
            with open(FATF_PREV_PATH) as f:
                p = json.load(f)
                prev["black"] = list(p.get("black", []))
                prev["grey"] = list(p.get("grey", []))
        except Exception:
            pass
    changes = {
        "black_added":   sorted(set(today["black"]) - set(prev["black"])),
        "black_removed": sorted(set(prev["black"]) - set(today["black"])),
        "grey_added":    sorted(set(today["grey"]) - set(prev["grey"])),
        "grey_removed": sorted(set(prev["grey"]) - set(today["grey"])),
        "current_black_count": len(today["black"]),
        "current_grey_count":  len(today["grey"]),
    }
    try:
        with open(FATF_PREV_PATH, "w") as f:
            json.dump(today, f, indent=2)
    except Exception as e:
        print(f"WARN: could not persist FATF previous: {e}", file=sys.stderr)
    return changes


def build_diff() -> dict:
    pre = _load_pre_counts()
    post = _current_counts()

    added, removed, zeroed, new_sources = [], [], [], []
    for sid, post_n in post.items():
        pre_n = pre.get(sid)
        if pre_n is None:
            new_sources.append({"source_id": sid, "count": post_n})
            continue
        delta = post_n - pre_n
        if delta > 0:
            added.append({"source_id": sid, "pre": pre_n, "post": post_n, "delta": delta})
        elif delta < 0:
            removed.append({"source_id": sid, "pre": pre_n, "post": post_n, "delta": delta})
    for sid, pre_n in pre.items():
        if pre_n > 0 and post.get(sid, 0) == 0:
            zeroed.append({"source_id": sid, "pre": pre_n, "post": 0})

    added.sort(key=lambda r: -r["delta"])
    removed.sort(key=lambda r: r["delta"])

    pre_total = sum(pre.values())
    post_total = sum(post.values())

    fatf_today = _fatf_today()
    fatf_changes = _fatf_changes(fatf_today)

    failed = _parse_failed_scrapers(_today_run_log())

    summary_lines: list[str] = []
    if added:
        top = ", ".join(f"{a['source_id']} +{a['delta']}" for a in added[:5])
        summary_lines.append(f"{len(added)} sources gained rows ({top})")
    if removed:
        summary_lines.append(f"{len(removed)} sources lost rows")
    if zeroed:
        summary_lines.append(f"{len(zeroed)} sources went to ZERO (probable scraper break)")
    if new_sources:
        summary_lines.append(f"{len(new_sources)} brand-new sources today")
    if failed:
        summary_lines.append(f"{len(failed)} scrapers reported failures")
    if fatf_changes["black_added"] or fatf_changes["grey_added"] or \
       fatf_changes["black_removed"] or fatf_changes["grey_removed"]:
        summary_lines.append("FATF list changed since last run")
    if not summary_lines:
        summary_lines.append("no per-source changes detected this run")

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "pre_total": pre_total,
        "post_total": post_total,
        "delta_total": post_total - pre_total,
        "sources_pre": len(pre),
        "sources_post": len(post),
        "added": added,
        "removed": removed,
        "zeroed": zeroed,
        "new_sources": new_sources,
        "failed_scrapers": failed,
        "fatf_changes": fatf_changes,
        "summary": summary_lines,
    }


def _print_human_summary(diff: dict) -> None:
    print("=== Pipeline change summary ===")
    print(f"  generated_at:     {diff['generated_at']}")
    print(f"  pre_total:        {diff['pre_total']:,}")
    print(f"  post_total:       {diff['post_total']:,}")
    print(f"  delta_total:      {diff['delta_total']:+,}")
    print(f"  sources (pre→post): {diff['sources_pre']} → {diff['sources_post']}")
    if diff["added"]:
        print(f"  +added ({len(diff['added'])}):")
        for r in diff["added"][:10]:
            print(f"     {r['source_id']:40s} {r['pre']:>9,} → {r['post']:>9,}  (+{r['delta']:,})")
    if diff["removed"]:
        print(f"  -removed ({len(diff['removed'])}):")
        for r in diff["removed"][:10]:
            print(f"     {r['source_id']:40s} {r['pre']:>9,} → {r['post']:>9,}  ({r['delta']:,})")
    if diff["zeroed"]:
        print(f"  !zeroed ({len(diff['zeroed'])}): {', '.join(z['source_id'] for z in diff['zeroed'][:5])}")
    if diff["new_sources"]:
        print(f"  *new ({len(diff['new_sources'])}): {', '.join(s['source_id'] for s in diff['new_sources'][:5])}")
    if diff["failed_scrapers"]:
        print(f"  failed_scrapers: {', '.join(diff['failed_scrapers'][:5])}")
    fc = diff["fatf_changes"]
    if any([fc["black_added"], fc["black_removed"], fc["grey_added"], fc["grey_removed"]]):
        print("  FATF changes:")
        for k in ("black_added", "black_removed", "grey_added", "grey_removed"):
            if fc[k]:
                print(f"     {k}: {', '.join(fc[k])}")
    print()
    for line in diff["summary"]:
        print(f"  - {line}")


def main() -> int:
    diff = build_diff()
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        with open(DIFF_PATH, "w") as f:
            json.dump(diff, f, indent=2)
        print(f"wrote {DIFF_PATH}")
    except Exception as e:
        print(f"WARN: could not write {DIFF_PATH}: {e}", file=sys.stderr)
    _print_human_summary(diff)
    return 0


if __name__ == "__main__":
    sys.exit(main())
