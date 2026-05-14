"""
utils/smart_change_detector.py — data-aware change detection.

The legacy detector in utils/change_detector.py hashes the raw HTML of
each source page and fires on every layout twitch (nav menu, ads,
generated timestamps). This module hashes only the *extracted* data
(the per-source CSV in data/) so it fires when the actual watchlist
changes, not when the page wrapper changes.

For each source we:

  1. Sort the CSV rows by (name, source_list).
  2. Build a content hash of the canonical rows.
  3. Compare against the previous snapshot at snapshots/<id>.json.
  4. Emit a list of (added, removed, modified) records and persist
     them to the watchlist_changes Postgres table.
  5. Drop a fresh snapshot.

CLI:
  python utils/smart_change_detector.py --source-id <id>
  python utils/smart_change_detector.py --all
  python utils/smart_change_detector.py --all --slack
  python utils/smart_change_detector.py --setup    # creates the PG table

run_all.sh calls this with --all between combine.py and the validator.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

try:
    import psycopg2
    from psycopg2.extras import execute_values
except Exception:
    psycopg2 = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SNAPSHOTS_DIR = os.path.join(PROJECT_ROOT, "snapshots", "data")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")

# Agencies whose changes get the HIGH-PRIORITY label.
HIGH_PRIORITY_AGENCIES = {
    "CBI", "Central Bureau of Investigation (CBI)",
    "National Investigation Agency (NIA)", "NIA",
    "Ministry of Home Affairs (MHA)", "MHA",
    "UNSC", "Interpol",
}

# Columns that count toward the "modified?" hash. We deliberately
# exclude scraped_at (always changes) and enrichment_status (mutated
# by post-processing).
COMPARE_FIELDS = (
    "case_unit", "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details", "has_document",
    "document_url", "detail_page_url", "interpol_notice_id",
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS watchlist_changes (
    id SERIAL PRIMARY KEY,
    source_id     VARCHAR(100),
    source_agency VARCHAR(200),
    source_list   VARCHAR(200),
    change_type   VARCHAR(20),
    entity_name   TEXT,
    old_value     TEXT,
    new_value     TEXT,
    detected_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS watchlist_changes_source_idx
    ON watchlist_changes (source_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS watchlist_changes_type_idx
    ON watchlist_changes (change_type, detected_at DESC);
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _db():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed")
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        user=os.environ.get("PG_USER", "aayush"),
        password=os.environ.get("PG_PASSWORD", "aayush123"),
        dbname=os.environ.get("PG_DB", "risk_pipeline"),
    )


def _load_sources():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("sources", [])


def _ensure_snapshots_dir():
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)


def _snapshot_path(source_id):
    return os.path.join(SNAPSHOTS_DIR, f"{source_id}.json")


def _row_key(row):
    """Stable identifier within a source: (name, source_list)."""
    return (row.get("name", "").strip().lower(),
            row.get("source_list", "").strip())


def _row_payload(row):
    """Canonical payload used to detect modifications. The case_unit
    (PAN/CIN/etc.) is the most stable cross-run anchor; address and
    details cover the rest."""
    parts = [str(row.get(k, "") or "").strip() for k in COMPARE_FIELDS]
    return "|".join(parts)


def _scan_csv(path):
    """Return dict {row_key: row_payload} and the row count."""
    if not os.path.exists(path):
        return {}, 0
    out = {}
    n = 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            k = _row_key(row)
            if not k[0]:
                continue
            out[k] = _row_payload(row)
            n += 1
    return out, n


def _save_snapshot(source_id, rows_dict, count):
    """Snapshot stores a content-hash plus per-key payload hashes so
    diff stays cheap on the next run."""
    payload_hashes = {f"{name}||{lst}": hashlib.sha256(v.encode("utf-8")).hexdigest()
                      for (name, lst), v in rows_dict.items()}
    bundle = {
        "source_id": source_id,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "row_count": count,
        "row_hashes": payload_hashes,
        "content_hash": _content_hash(rows_dict),
    }
    _ensure_snapshots_dir()
    with open(_snapshot_path(source_id), "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)


def _load_snapshot(source_id):
    p = _snapshot_path(source_id)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _content_hash(rows_dict):
    """Hash all (name, list, payload) tuples deterministically."""
    h = hashlib.sha256()
    for key in sorted(rows_dict.keys()):
        name, lst = key
        h.update(name.encode("utf-8"))
        h.update(b"|")
        h.update(lst.encode("utf-8"))
        h.update(b"|")
        h.update(rows_dict[key].encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------
def detect_changes(source_id, csv_path, source_agency, source_list):
    """Compare current CSV against previous snapshot. Return dict with
    keys: source_id, source_agency, source_list, prev_count, new_count,
    added, removed, modified (each a list of dicts)."""
    current_rows, new_count = _scan_csv(csv_path)
    snap = _load_snapshot(source_id)

    if snap is None:
        return {
            "source_id": source_id,
            "source_agency": source_agency,
            "source_list": source_list,
            "first_run": True,
            "prev_count": 0,
            "new_count": new_count,
            "added": [],
            "removed": [],
            "modified": [],
        }

    prev_hashes = snap.get("row_hashes", {})
    prev_count = snap.get("row_count", 0)
    added, removed, modified = [], [], []
    current_keys = set(current_rows.keys())
    # rebuild prev_keys from "name||list" string keys
    prev_keys = set()
    prev_payload_h = {}
    for k, v in prev_hashes.items():
        parts = k.split("||", 1)
        if len(parts) == 2:
            prev_keys.add((parts[0], parts[1]))
            prev_payload_h[(parts[0], parts[1])] = v

    for k in current_keys - prev_keys:
        added.append({"entity_name": k[0], "source_list": k[1]})
    for k in prev_keys - current_keys:
        removed.append({"entity_name": k[0], "source_list": k[1]})
    for k in current_keys & prev_keys:
        new_payload = current_rows[k]
        new_h = hashlib.sha256(new_payload.encode("utf-8")).hexdigest()
        if new_h != prev_payload_h.get(k):
            modified.append({"entity_name": k[0], "source_list": k[1]})
    return {
        "source_id": source_id,
        "source_agency": source_agency,
        "source_list": source_list,
        "first_run": False,
        "prev_count": prev_count,
        "new_count": new_count,
        "added": added,
        "removed": removed,
        "modified": modified,
    }, current_rows, new_count


def _persist_changes(diff, conn):
    rows = []
    sid = diff["source_id"]
    ag = diff["source_agency"]
    lst = diff["source_list"]
    if diff["prev_count"] and diff["new_count"] != diff["prev_count"]:
        rows.append((sid, ag, lst, "count_change", None,
                     str(diff["prev_count"]), str(diff["new_count"])))
    for r in diff["added"]:
        rows.append((sid, ag, lst, "added", r["entity_name"], None, r["source_list"]))
    for r in diff["removed"]:
        rows.append((sid, ag, lst, "removed", r["entity_name"], r["source_list"], None))
    for r in diff["modified"]:
        rows.append((sid, ag, lst, "modified", r["entity_name"], None, r["source_list"]))
    if not rows:
        return 0
    with conn.cursor() as cur:
        execute_values(cur,
            "INSERT INTO watchlist_changes "
            "(source_id, source_agency, source_list, change_type, entity_name, old_value, new_value) "
            "VALUES %s", rows)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def run_source(source_id, source_agency, source_list, conn=None):
    csv_path = os.path.join(DATA_DIR, f"{source_id}.csv")
    if not os.path.exists(csv_path):
        return None
    result = detect_changes(source_id, csv_path, source_agency, source_list)
    if isinstance(result, tuple):
        diff, current_rows, new_count = result
    else:
        diff = result
        current_rows, _ = _scan_csv(csv_path)
        new_count = diff["new_count"]
    persisted = 0
    if conn is not None and not diff.get("first_run"):
        persisted = _persist_changes(diff, conn)
    _save_snapshot(source_id, current_rows, new_count)
    diff["persisted"] = persisted
    return diff


def run_all(slack=False):
    sources = _load_sources()
    active = [s for s in sources if s.get("status") == "active"]
    conn = _db()
    grand = {"added": 0, "removed": 0, "modified": 0,
             "count_change": 0, "first_run": 0, "unchanged": 0}
    notable = []           # for the slack/report
    high_pri = []          # HIGH-PRIORITY changes
    for s in active:
        sid = s.get("id")
        if not sid:
            continue
        diff = run_source(sid, s.get("agency"), s.get("list_name"), conn=conn)
        if diff is None:
            continue
        if diff.get("first_run"):
            grand["first_run"] += 1
            continue
        n_add, n_rem, n_mod = len(diff["added"]), len(diff["removed"]), len(diff["modified"])
        if n_add == n_rem == n_mod == 0 and diff["new_count"] == diff["prev_count"]:
            grand["unchanged"] += 1
            continue
        grand["added"] += n_add
        grand["removed"] += n_rem
        grand["modified"] += n_mod
        if diff["new_count"] != diff["prev_count"]:
            grand["count_change"] += 1
        notable.append(diff)
        # High-priority flags:
        if s.get("agency") in HIGH_PRIORITY_AGENCIES:
            if n_add:
                high_pri.append((s["agency"], s["list_name"], "added", n_add))
            if n_rem:
                high_pri.append((s["agency"], s["list_name"], "removed", n_rem))
        elif n_rem:
            high_pri.append((s.get("agency"), s.get("list_name"), "removed", n_rem))
        # Large drop = scraper likely broke
        if diff["prev_count"]:
            drop = (diff["prev_count"] - diff["new_count"]) / max(1, diff["prev_count"])
            if drop > 0.10:
                high_pri.append((s.get("agency"), s.get("list_name"),
                                 "large_drop",
                                 f"{int(drop*100)}% ({diff['prev_count']}→{diff['new_count']})"))
    conn.close()

    _print_report(grand, notable, high_pri)
    if slack and (grand["added"] or grand["removed"] or grand["modified"]):
        send_change_alert(grand, high_pri)
    return grand


def _print_report(grand, notable, high_pri):
    print("=" * 60)
    print("SMART CHANGE DETECTION REPORT")
    print("=" * 60)
    print(f"Across active sources:  +{grand['added']} new  "
          f"-{grand['removed']} removed  ~{grand['modified']} modified  "
          f"({grand['count_change']} sources with count change)")
    print(f"First run snapshots:    {grand['first_run']}")
    print(f"Unchanged sources:      {grand['unchanged']}")
    if high_pri:
        print("\nHIGH-PRIORITY CHANGES:")
        for ag, lst, kind, n in high_pri[:40]:
            print(f"  [{kind:<11}] {ag} — {lst}: {n}")
    if notable:
        print("\nChanged sources (top 20 by total activity):")
        notable.sort(key=lambda d: -(len(d["added"])+len(d["removed"])+len(d["modified"])))
        for d in notable[:20]:
            print(f"  {d['source_agency']:<35} {d['source_list'][:45]:<45}  "
                  f"+{len(d['added'])} -{len(d['removed'])} ~{len(d['modified'])}  "
                  f"({d['prev_count']}->{d['new_count']})")
            for r in d["added"][:3]:
                print(f"     + {r['entity_name'][:80]}")
            for r in d["removed"][:3]:
                print(f"     - {r['entity_name'][:80]}")


def send_change_alert(grand, high_pri):
    try:
        from utils.notifier import send_slack_message, _section, _header
    except Exception as e:
        print(f"  Slack alert skipped: {e}")
        return
    parts = [
        f"+{grand['added']} added · -{grand['removed']} removed · "
        f"~{grand['modified']} modified across {grand['count_change']} sources"
    ]
    for ag, lst, kind, n in high_pri[:8]:
        parts.append(f"• `{kind}` — {ag} / {lst[:40]}: {n}")
    blocks = [_header("🔔 AML Watchlist — Changes Detected"),
              _section("\n".join(parts))]
    fallback = parts[0]
    try:
        send_slack_message(blocks, fallback)
    except Exception as e:
        print(f"  Slack post failed: {e}")


def setup():
    conn = _db()
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
    conn.commit()
    conn.close()
    print("OK: watchlist_changes table ready")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-id", help="single source id to check")
    ap.add_argument("--all", action="store_true",
                    help="run over every active source")
    ap.add_argument("--slack", action="store_true",
                    help="post a Slack alert if any changes detected")
    ap.add_argument("--setup", action="store_true",
                    help="create the watchlist_changes table and exit")
    args = ap.parse_args()
    if args.setup:
        setup(); return
    if args.source_id:
        sources = {s.get("id"): s for s in _load_sources()}
        s = sources.get(args.source_id)
        if not s:
            print(f"unknown source-id: {args.source_id}")
            sys.exit(2)
        conn = _db()
        diff = run_source(s["id"], s.get("agency"), s.get("list_name"), conn=conn)
        conn.close()
        print(json.dumps({k: (v if not isinstance(v, list) else v[:5])
                          for k, v in diff.items()}, indent=2))
        return
    if args.all:
        run_all(slack=args.slack)
        return
    ap.error("pick --source-id, --all, or --setup")


if __name__ == "__main__":
    main()
