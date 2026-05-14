"""
Production data-quality gate.

Runs after every daily scrape (from run_all.sh) and surfaces issues
*before* anyone trusts the new data. Three findings buckets:

  CRITICAL   = data integrity broken; humans must look. Validator
               exits non-zero so cron sees it.
  WARNING    = looks like the source changed shape (record count
               drop, suspicious patterns) — investigate next run.
  INFO       = noteworthy but not actionable (record-count
               increase, broad cross-source overlap).

Checks performed (per spec):
  1. Schema      — every data/*.csv has the canonical 17 columns and
                   at least 1 row.
  2. Name quality — empty / digit-only / too-long / HTML-tagged /
                    garbage-prefix names.
  3. Record-count monitoring — actual vs sources.json
                    `expected_min_records`:
                     0 rows for a previously-active source → CRITICAL
                     actual < 0.8 * expected_min          → WARNING
                     actual > 2.0 * expected_min          → INFO
  4. Duplicates  — exact (name, source_list) repeats; entities
                   appearing in 5+ different sources.
  5. Freshness   — any source whose latest scraped_at is older than
                   7 days.

CLI:
  python scripts/validate_production.py
  python scripts/validate_production.py --report reports/X.csv
  python scripts/validate_production.py --slack    # post to Slack
  python scripts/validate_production.py --email    # email a copy

Exit codes:
  0  no CRITICAL findings
  1  one or more CRITICAL findings
"""

import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

try:
    import psycopg2
except Exception:
    psycopg2 = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")

EXPECTED_HEADERS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# Generic UI/navigation prefixes that have leaked into the name field
# from past JS-stub captures. Used in the name-quality check.
GARBAGE_PREFIXES = (
    "home", "menu", "click", "login", "copyright",
    "skip", "search", "back", "next", "previous", "page",
    "error", "loading", "undefined", "null",
)

HTML_TAG_RE = re.compile(r"<[a-zA-Z/!?][^>]*>")
DIGIT_ONLY_RE = re.compile(r"^\s*[\d.,\s-]+$")


# ---------- helpers --------------------------------------------------------
def _load_sources():
    if not os.path.exists(SOURCES_PATH):
        return []
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("sources", [])


def _today_ist():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _add(findings, severity, source, message):
    findings.append({
        "severity": severity, "source": source, "message": message,
    })


# ---------- check 1: schema ------------------------------------------------
def check_schema(csv_path, findings):
    name = os.path.basename(csv_path)
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            n_rows = sum(1 for _ in reader)
    except Exception as e:
        _add(findings, "CRITICAL", name,
             f"schema: failed to read CSV ({type(e).__name__}: {e})")
        return None
    if headers != EXPECTED_HEADERS:
        missing = set(EXPECTED_HEADERS) - set(headers or [])
        extra = set(headers or []) - set(EXPECTED_HEADERS)
        _add(findings, "CRITICAL", name,
             f"schema: headers mismatch (missing={sorted(missing)} extra={sorted(extra)})")
    if n_rows == 0:
        # Empty CSVs are normal for sources that legitimately publish
        # nothing today (e.g., NCDEX cessation/expelled — currently blank).
        # The record-count check separately upgrades this to CRITICAL
        # when the source has an `expected_min_records` > 0.
        _add(findings, "WARNING", name, "schema: 0 data rows")
    return n_rows


# ---------- check 2: name quality ------------------------------------------
def check_names(csv_path, findings, is_active):
    """Name-quality findings. The `is_active` flag controls severity:
    a single empty name in a >100-row CSV is downgraded to WARNING
    because it's usually a stray header artefact; the same finding in
    an *inactive* source is downgraded to INFO since nobody's reading
    that CSV anyway."""
    name = os.path.basename(csv_path)
    empty = digit_only = too_short = too_long = with_html = garbage_prefix = 0
    total = 0
    sample_bad = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "name" not in (reader.fieldnames or []):
            return
        for r in reader:
            total += 1
            nm = (r.get("name") or "").strip()
            if not nm:
                empty += 1
                continue
            if DIGIT_ONLY_RE.match(nm):
                digit_only += 1
                if len(sample_bad) < 3:
                    sample_bad.append(("digit_only", nm[:80]))
                continue
            if len(nm) < 2:
                too_short += 1
                continue
            if len(nm) > 200:
                too_long += 1
                if len(sample_bad) < 3:
                    sample_bad.append(("len>200", nm[:80]))
            if HTML_TAG_RE.search(nm):
                with_html += 1
                if len(sample_bad) < 3:
                    sample_bad.append(("html_tag", nm[:80]))
            low = nm.lower()
            if any(low.startswith(p + " ") or low == p or low.startswith(p + "|")
                   for p in GARBAGE_PREFIXES):
                garbage_prefix += 1
                if len(sample_bad) < 3:
                    sample_bad.append(("garbage_prefix", nm[:80]))

    # Empty: a single empty row in a populated CSV is usually a header
    # artefact, not a data problem — downgrade to WARNING/INFO.
    if empty:
        if not is_active:
            _add(findings, "INFO", name,
                 f"name_quality: {empty} empty names (inactive source)")
        elif total == 0 or empty / max(1, total) > 0.05:
            _add(findings, "CRITICAL", name,
                 f"name_quality: {empty} empty names "
                 f"({empty}/{total} = {empty/max(1,total)*100:.1f}%)")
        else:
            _add(findings, "WARNING", name,
                 f"name_quality: {empty} empty name(s) in {total} rows")
    # Digit-only and HTML tags inside names are always bad.
    if digit_only:
        sev = "CRITICAL" if is_active else "INFO"
        _add(findings, sev, name,
             f"name_quality: {digit_only} digit-only names "
             f"(sample: {sample_bad[0][1] if sample_bad else '?'})")
    if with_html:
        sev = "CRITICAL" if is_active else "INFO"
        _add(findings, sev, name,
             f"name_quality: {with_html} names contain HTML tags")
    if garbage_prefix:
        _add(findings, "WARNING", name,
             f"name_quality: {garbage_prefix} names start with garbage prefix")
    if too_long:
        _add(findings, "WARNING", name,
             f"name_quality: {too_long} names longer than 200 chars")


# ---------- check 3: record-count monitoring -------------------------------
def check_record_counts(per_file_counts, sources, findings):
    """Map each source to its CSV-on-disk count and compare to
    `expected_min_records`. The CSV path is inferred from the source id
    (sources.json `id` matches the CSV filename stem)."""
    by_id = {s.get("id"): s for s in sources if s.get("id")}
    name_to_file = {}
    for fn in per_file_counts:
        stem = fn.replace(".csv", "")
        name_to_file[stem] = fn

    seen_ids = set()
    for sid, src in by_id.items():
        if src.get("status") != "active":
            continue
        seen_ids.add(sid)
        fn = name_to_file.get(sid)
        actual = per_file_counts.get(fn, 0) if fn else 0
        expected_min = src.get("expected_min_records") or 0
        list_name = src.get("list_name", sid)
        if fn is None:
            _add(findings, "CRITICAL", sid,
                 f"record_count: source is 'active' but no CSV found "
                 f"(expected file: data/{sid}.csv)")
            continue
        if actual == 0 and expected_min > 0:
            _add(findings, "CRITICAL", fn,
                 f"record_count: 0 rows for active source '{list_name}' "
                 f"(expected_min={expected_min})")
        elif expected_min > 0:
            if actual < 0.8 * expected_min:
                _add(findings, "WARNING", fn,
                     f"record_count: {actual} rows < 80% of expected_min "
                     f"({expected_min}) for '{list_name}'")
            elif actual > 2.0 * expected_min:
                _add(findings, "INFO", fn,
                     f"record_count: {actual} rows > 2x expected_min "
                     f"({expected_min}) for '{list_name}' — review baseline")


# ---------- check 4: duplicates -------------------------------------------
def check_duplicates(csv_paths, findings):
    cross_source = defaultdict(set)   # name_lc -> set of source_lists
    for path in csv_paths:
        fn = os.path.basename(path)
        per_source = Counter()
        with open(path, "r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                nm = (r.get("name") or "").strip()
                if not nm:
                    continue
                key = nm.lower()
                per_source[key] += 1
                cross_source[key].add(r.get("source_list") or fn)
        dup_count = sum(1 for c in per_source.values() if c > 1)
        if dup_count:
            top = max(per_source.items(), key=lambda kv: kv[1])
            _add(findings, "INFO", fn,
                 f"duplicates: {dup_count} duplicate names in source "
                 f"(top: '{top[0][:60]}' x{top[1]})")
    wide = [(k, len(v)) for k, v in cross_source.items() if len(v) >= 5]
    wide.sort(key=lambda kv: -kv[1])
    for nm, n_lists in wide[:5]:
        _add(findings, "INFO", "<cross-source>",
             f"duplicates: '{nm[:60]}' appears in {n_lists} different source_lists")


# ---------- check 5: freshness --------------------------------------------
def check_freshness(csv_paths, findings, threshold_days=7):
    now = datetime.now()
    for path in csv_paths:
        fn = os.path.basename(path)
        latest = None
        with open(path, "r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                ts = (r.get("scraped_at") or "").strip()
                if not ts:
                    continue
                try:
                    dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if latest is None or dt > latest:
                    latest = dt
        if latest is None:
            _add(findings, "WARNING", fn,
                 "freshness: no parseable scraped_at on any row")
            continue
        age_days = (now - latest).days
        if age_days > threshold_days:
            _add(findings, "WARNING", fn,
                 f"freshness: latest scraped_at is {age_days} days old "
                 f"({latest:%Y-%m-%d})")


# ---------- summary / output ----------------------------------------------
def _save_report(findings, report_path):
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["severity", "source", "message"])
        for f_ in findings:
            w.writerow([f_["severity"], f_["source"], f_["message"]])


def _print_report(findings, n_files, total_rows):
    sev_counts = Counter(f["severity"] for f in findings)
    print("=" * 60)
    print("PRODUCTION VALIDATION REPORT")
    print("=" * 60)
    print(f"Timestamp:          {_today_ist():%Y-%m-%d %H:%M:%S} IST")
    print(f"Total CSVs checked: {n_files}")
    print(f"Total rows on disk: {total_rows:,}")
    print()
    for sev in ("CRITICAL", "WARNING", "INFO"):
        items = [f for f in findings if f["severity"] == sev]
        print(f"{sev}: {len(items)}")
        for f in items[:30]:
            print(f"   [{f['source']:<55}] {f['message']}")
        if len(items) > 30:
            print(f"   ... + {len(items) - 30} more")
        print()
    return sev_counts


# ---------- main -----------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=None,
                    help="path to write the CSV report (default: stdout only)")
    ap.add_argument("--slack", action="store_true",
                    help="post the report summary to Slack via notifier")
    ap.add_argument("--email", action="store_true",
                    help="email the report via notifier")
    args = ap.parse_args()

    csv_paths = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))
    csv_paths = [p for p in csv_paths
                 if not os.path.basename(p).startswith("master_")]
    findings = []
    per_file_counts = {}
    total_rows = 0

    sources = _load_sources()
    active_stems = {s.get("id") for s in sources if s.get("status") == "active"}

    for p in csv_paths:
        fn = os.path.basename(p)
        stem = fn[:-4] if fn.endswith(".csv") else fn
        is_active = stem in active_stems
        n = check_schema(p, findings)
        if n is None:
            continue
        per_file_counts[fn] = n
        total_rows += n
        check_names(p, findings, is_active)
    check_record_counts(per_file_counts, sources, findings)
    check_duplicates(csv_paths, findings)
    check_freshness(csv_paths, findings)

    sev = _print_report(findings, n_files=len(csv_paths), total_rows=total_rows)

    if args.report:
        _save_report(findings, args.report)
        print(f"Saved report: {args.report}")

    if args.slack or args.email:
        try:
            from utils.notifier import send_alert  # type: ignore
        except Exception as e:
            print(f"  notifier import failed: {e}")
            send_alert = None
        if send_alert is not None:
            summary = (f"Validator: CRITICAL={sev.get('CRITICAL',0)}  "
                       f"WARNING={sev.get('WARNING',0)}  "
                       f"INFO={sev.get('INFO',0)}  "
                       f"({len(csv_paths)} CSVs, {total_rows:,} rows)")
            try:
                send_alert(summary,
                           severity="error" if sev.get("CRITICAL") else "info")
            except Exception as e:
                print(f"  notifier send failed: {e}")

    sys.exit(1 if sev.get("CRITICAL", 0) > 0 else 0)


if __name__ == "__main__":
    main()
