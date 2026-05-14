"""
scripts/validate_schema.py

Per-source CSV validator. Flags rows where:
  * column count != 17 (or 18 with optional confidence col)
  * 'name' is empty / a serial number / a navigation menu phrase
  * 'source_agency' is empty
  * 'scraped_at' is missing or unparseable
  * the row is an exact duplicate of an earlier row in the same file

Output: a stdout report and reports/schema_validation.csv.

Sources where >10% of rows fail validation are flagged loudly so they
can be reviewed before they propagate into the master CSV.

Usage:
    python -m scripts.validate_schema
    python -m scripts.validate_schema --json
    python -m scripts.validate_schema --source <source_id>
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
REPORT_PATH = os.path.join(REPORTS_DIR, "schema_validation.csv")

CANONICAL_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

NAV_TERMS = {"home", "about", "contact", "login", "search", "menu",
             "back", "next", "previous", "submit", "click here",
             "read more", "download", "skip to content"}

FAIL_THRESHOLD = 0.10  # >10% rows failing -> loud flag


def _is_serial_number(s):
    s = (s or "").strip()
    return bool(s) and (s.isdigit() or re.fullmatch(r"\d+\.?", s) is not None
                        or len(s) <= 2)


def _is_nav_phrase(s):
    s = (s or "").strip().lower()
    if not s:
        return False
    return s in NAV_TERMS


def _ts_ok(s):
    s = (s or "").strip()
    if not s:
        return False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False


def validate_csv(path, source_id):
    """Return dict of per-source validation results."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return {"source_id": source_id, "total_rows": 0, "valid_rows": 0,
                    "issues": ["empty_file"], "fail_pct": 0.0}
        rows = list(reader)

    issues = []
    if header[:17] != CANONICAL_FIELDS:
        issues.append(f"header_mismatch: {header[:17]}")
    if len(header) not in (17, 18):
        issues.append(f"col_count={len(header)}")

    # Mapping by name in case header has the optional 18th confidence col.
    col_index = {h: i for i, h in enumerate(header)}
    seen_keys = set()
    valid = 0
    bad_reasons = {}

    def bump(reason):
        bad_reasons[reason] = bad_reasons.get(reason, 0) + 1

    for row in rows:
        # Pad short rows so we never IndexError.
        while len(row) < len(header):
            row.append("")
        cell = lambda k: (row[col_index.get(k, -1)]
                          if col_index.get(k, -1) >= 0 else "")
        problems = []
        if len(row) not in (17, 18):
            problems.append("col_count")
        name = (cell("name") or "").strip()
        if not name:
            problems.append("empty_name")
        elif _is_serial_number(name):
            problems.append("name_is_serial")
        elif _is_nav_phrase(name):
            problems.append("name_is_nav")
        if not (cell("source_agency") or "").strip():
            problems.append("empty_agency")
        if not _ts_ok(cell("scraped_at")):
            problems.append("bad_scraped_at")
        # Duplicate detection within the same file.
        dup_key = (name.lower(), (cell("source_list") or "").lower(),
                   (cell("address") or "").lower())
        if dup_key in seen_keys:
            problems.append("duplicate_within_source")
        else:
            seen_keys.add(dup_key)

        if not problems:
            valid += 1
        else:
            for p in problems:
                bump(p)

    total = len(rows)
    fail_pct = 0.0 if total == 0 else (total - valid) / total
    return {
        "source_id": source_id,
        "total_rows": total,
        "valid_rows": valid,
        "fail_pct": round(fail_pct, 3),
        "issues": ", ".join(f"{k}:{v}" for k, v in sorted(
            bad_reasons.items(), key=lambda x: -x[1])) or ("ok" if not issues else ""),
        "header_issues": "; ".join(issues),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--source", help="validate only this source id")
    args = ap.parse_args()

    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)["sources"]

    rows = []
    for s in sources:
        sid = s["id"]
        if args.source and sid != args.source:
            continue
        path = os.path.join(DATA_DIR, f"{sid}.csv")
        if not os.path.exists(path):
            continue
        rows.append(validate_csv(path, sid))

    # Sort: highest fail_pct first.
    rows.sort(key=lambda r: -r["fail_pct"])

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source_id", "total_rows",
                                          "valid_rows", "fail_pct",
                                          "issues", "header_issues"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    flagged = [r for r in rows if r["fail_pct"] > FAIL_THRESHOLD]
    print(f"Validated {len(rows)} CSVs. {len(flagged)} flagged "
          f"(>{FAIL_THRESHOLD*100:.0f}% rows fail).")
    print(f"Full report: {REPORT_PATH}\n")
    print(f"{'source_id':<48} {'rows':>5} {'valid':>5} {'fail%':>6}  issues")
    print("-" * 110)
    for r in rows[:25]:
        print(f"{r['source_id'][:48]:<48} "
              f"{r['total_rows']:>5} {r['valid_rows']:>5} "
              f"{r['fail_pct']*100:>5.1f}%  {r['issues'][:60]}")
    if len(rows) > 25:
        print(f"  ... +{len(rows) - 25} more in {REPORT_PATH}")


if __name__ == "__main__":
    main()
