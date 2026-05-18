#!/usr/bin/env python3
"""
monitor_sources.py — Source Health Monitor for the Overwatch AML pipeline.

Snapshots per-source row counts + last-scraped into source_health, compares
against the most recent prior snapshot, classifies each source
(OK / BROKEN / ANOMALY / STALE / NEW), writes a daily report, and sends a
consolidated Slack alert if anything is BROKEN or ANOMALY.

CLI:
  python scripts/monitor_sources.py                 full run
  python scripts/monitor_sources.py --snapshot-only just snapshot
  python scripts/monitor_sources.py --report-only   compare last two snapshots
  python scripts/monitor_sources.py --dry-run       no Slack; print alert instead
"""
import argparse
import datetime as dt
import json
import logging
import os
import re
import sys

import psycopg2
import psycopg2.extras
import requests

PROJECT = "/home/aayush/risk-pipeline"
LOG_DIR = os.path.join(PROJECT, "logs")
SETTINGS = os.path.join(PROJECT, ".claude", "settings.local.json")
DB = dict(host="localhost", user="aayush", password="aayush123", dbname="risk_pipeline")

STALE_DAYS = 7
ANOMALY_DROP_PCT = 50.0

os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("monitor_sources")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_sh = logging.StreamHandler(sys.stdout); _sh.setFormatter(_fmt)
_fh = logging.FileHandler(os.path.join(LOG_DIR, "monitor.log")); _fh.setFormatter(_fmt)
logger.addHandler(_sh); logger.addHandler(_fh)


def connect():
    return psycopg2.connect(**DB)


def find_slack_webhook():
    """settings.local.json (any key/string containing a hooks.slack.com URL),
    then SLACK_WEBHOOK_URL env. Returns URL or None."""
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            raw = f.read()
        m = re.search(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+", raw)
        if m:
            return m.group(0)
    except Exception as e:
        logger.warning("Could not read settings.local.json: %s", e)
    env = os.environ.get("SLACK_WEBHOOK_URL")
    if env:
        return env
    return None


def take_snapshot(conn):
    """Upsert today's per-source row_count + last_scraped into source_health."""
    today = dt.date.today()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_id,
                   MAX(source_agency), MAX(source_list),
                   COUNT(*)::int, MAX(scraped_at)
            FROM watchlist_records
            GROUP BY source_id
        """)
        rows = cur.fetchall()
        for sid, agency, slist, cnt, last in rows:
            cur.execute("""
                INSERT INTO source_health
                    (source_id, source_agency, source_list, row_count,
                     last_scraped, snapshot_date, status)
                VALUES (%s,%s,%s,%s,%s,%s,'OK')
                ON CONFLICT (source_id, snapshot_date) DO UPDATE
                  SET row_count=EXCLUDED.row_count,
                      last_scraped=EXCLUDED.last_scraped,
                      source_agency=EXCLUDED.source_agency,
                      source_list=EXCLUDED.source_list
            """, (sid, agency, slist, cnt, last, today))
    conn.commit()
    logger.info("Snapshot for %s: %d sources recorded", today, len(rows))
    return len(rows)


def get_snapshot(conn, snapshot_date):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT source_id, source_agency, source_list, row_count,
                              last_scraped
                       FROM source_health WHERE snapshot_date=%s""",
                    (snapshot_date,))
        return {r["source_id"]: r for r in cur.fetchall()}


def last_two_snapshot_dates(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT snapshot_date FROM source_health "
                    "ORDER BY snapshot_date DESC LIMIT 2")
        return [r[0] for r in cur.fetchall()]


def classify(curr, prev):
    """Return (status, note) for one source comparing current vs previous."""
    now = dt.datetime.now()
    last = curr.get("last_scraped")
    cnt = curr["row_count"]
    if prev is None:
        return "NEW", "first appearance in snapshots"
    old = prev["row_count"]
    if old > 0 and cnt == 0:
        return "BROKEN", f"row count {old} -> 0"
    if old > 0 and cnt < old:
        drop = (old - cnt) / old * 100.0
        if drop > ANOMALY_DROP_PCT:
            return "ANOMALY", f"row count dropped {drop:.0f}% ({old} -> {cnt})"
    if last is not None:
        age = (now - last).days
        if age > STALE_DAYS:
            return "STALE", f"last scraped {age} days ago"
    return "OK", ""


def build_report(conn, curr_date, prev_date):
    curr = get_snapshot(conn, curr_date)
    prev = get_snapshot(conn, prev_date) if prev_date else {}
    results = []
    for sid, c in curr.items():
        status, note = classify(c, prev.get(sid))
        results.append({
            "source_id": sid,
            "agency": c["source_agency"], "list": c["source_list"],
            "prev": prev.get(sid, {}).get("row_count"),
            "curr": c["row_count"],
            "last_scraped": c["last_scraped"], "status": status, "note": note,
        })
    # Persist status back onto today's snapshot rows
    with conn.cursor() as cur:
        for r in results:
            cur.execute("UPDATE source_health SET status=%s, notes=%s "
                        "WHERE source_id=%s AND snapshot_date=%s",
                        (r["status"], r["note"], r["source_id"], curr_date))
    conn.commit()
    return results


def write_report_file(results, curr_date, prev_date):
    by = lambda s: [r for r in results if r["status"] == s]
    broken, anomaly, stale, new, ok = (by("BROKEN"), by("ANOMALY"),
                                       by("STALE"), by("NEW"), by("OK"))
    path = os.path.join(LOG_DIR,
                        f"monitor_report_{curr_date.strftime('%Y%m%d')}.txt")
    srt = sorted(results, key=lambda r: r["curr"], reverse=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"AML Source Health Report — {curr_date}\n")
        f.write(f"(compared against snapshot: {prev_date or 'NONE — first run'})\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Total sources : {len(results)}\n")
        f.write(f"OK            : {len(ok)}\n")
        f.write(f"BROKEN        : {len(broken)}\n")
        f.write(f"ANOMALY       : {len(anomaly)}\n")
        f.write(f"STALE         : {len(stale)}\n")
        f.write(f"NEW           : {len(new)}\n\n")
        if broken or anomaly or stale or new:
            f.write("FLAGGED SOURCES\n" + "-" * 70 + "\n")
            for r in broken + anomaly + stale + new:
                f.write(f"[{r['status']}] {r['agency']} / {r['list']} "
                        f"({r['source_id']})\n"
                        f"    prev={r['prev']} curr={r['curr']} "
                        f"last_scraped={r['last_scraped']}  {r['note']}\n")
            f.write("\n")
        f.write("TOP 10 BY ROW COUNT\n" + "-" * 70 + "\n")
        for r in srt[:10]:
            f.write(f"  {r['curr']:>10}  {r['agency']} / {r['list']}\n")
        f.write("\nBOTTOM 10 BY ROW COUNT\n" + "-" * 70 + "\n")
        for r in srt[-10:]:
            f.write(f"  {r['curr']:>10}  {r['agency']} / {r['list']}\n")
    logger.info("Report written: %s", path)
    return path, dict(total=len(results), ok=len(ok), broken=len(broken),
                      anomaly=len(anomaly), stale=len(stale), new=len(new))


def send_slack(results, curr_date, dry_run):
    broken = [r for r in results if r["status"] == "BROKEN"]
    anomaly = [r for r in results if r["status"] == "ANOMALY"]
    stale = [r for r in results if r["status"] == "STALE"]
    if not (broken or anomaly):
        logger.info("No BROKEN/ANOMALY sources — no Slack alert needed.")
        return
    lines = [f"🚨 AML Source Monitor Alert — {curr_date}",
             f"BROKEN: {len(broken)} sources (row count dropped to 0)",
             f"ANOMALY: {len(anomaly)} sources (row count dropped >50%)",
             f"STALE: {len(stale)} sources (not scraped in 7+ days)",
             "", "Flagged sources:"]
    for r in (broken + anomaly)[:40]:
        lines.append(f"• {r['agency']} / {r['list']} — {r['status']} "
                     f"(was {r['prev']} → now {r['curr']})")
    msg = "\n".join(lines)
    if dry_run:
        logger.info("[DRY-RUN] Slack message:\n%s", msg)
        return
    url = find_slack_webhook()
    if not url:
        logger.warning("No Slack webhook found (settings.local.json / env) "
                        "— skipping alert.")
        return
    try:
        resp = requests.post(url, json={"text": msg}, timeout=15)
        if resp.status_code == 200:
            logger.info("Slack alert sent (%d flagged).", len(broken)+len(anomaly))
        else:
            logger.error("Slack POST failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Slack POST exception: %s", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot-only", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.snapshot_only:
            take_snapshot(conn)
            logger.info("Snapshot-only run complete.")
            return

        if not args.report_only:
            take_snapshot(conn)

        dates = last_two_snapshot_dates(conn)
        if not dates:
            logger.error("No snapshots in source_health — run --snapshot-only first.")
            return
        curr_date = dates[0]
        prev_date = dates[1] if len(dates) > 1 else None

        results = build_report(conn, curr_date, prev_date)
        path, summ = write_report_file(results, curr_date, prev_date)
        logger.info("Summary %s", summ)
        send_slack(results, curr_date, args.dry_run)
        logger.info("Done. Report: %s", path)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
