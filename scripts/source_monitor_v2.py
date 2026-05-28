#!/usr/bin/env python3
"""
scripts/source_monitor_v2.py — Enterprise source health monitor.

Replaces the legacy utils/smart_change_detector.py call in run_all.sh. The old
detector only compared CSVs that the scrapers had successfully produced — it
was silent when a scraper FAILED to refresh (yesterday's CSV stays put and
looks "unchanged"). This monitor checks every source from the outside as well:

  * HTTP health        — HEAD on every source URL (5s timeout). Catches DNS
                         failures, 5xx, captcha walls, 403s, redirects.
  * Content hash       — GET on change_detection sources, hash the body
                         (CSRF/dynamic-chrome stripped). Catches "upstream
                         updated the data but our scraper didn't pick it up".
  * Layout fingerprint — for the 12 sources with a CSS selector, count
                         elements matching it. Catches "upstream redesigned
                         and our extractor would break next run".
  * Row count snapshot — from source_health table — pre→post deltas.
  * Staleness          — MAX(scraped_at) per source (7d warn, 30d alarm).
  * Bulk-source freshness — OpenSanctions `last_change`, FATF list diff.

Output: logs/source_monitor_v2.json (a structured report the daily email/
Slack message and /api/pipeline/status all consume).

Persistent state, used to detect change since last run:
  logs/content_hashes.json
  logs/layout_fingerprints.json

CLI:
  source_monitor_v2.py --all                  full run, all 942 sources
  source_monitor_v2.py --data-only            skip HTTP, just DB+CSV checks
  source_monitor_v2.py --source <id>          debug a single source
  source_monitor_v2.py --dry-run              don't post Slack
  source_monitor_v2.py --slack                post Slack on any alert
  source_monitor_v2.py --verbose              chatty logging
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")
REPORT_PATH = os.path.join(LOG_DIR, "source_monitor_v2.json")
HASHES_PATH = os.path.join(LOG_DIR, "content_hashes.json")
LAYOUT_PATH = os.path.join(LOG_DIR, "layout_fingerprints.json")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

HEAD_TIMEOUT = 5
GET_TIMEOUT = 15
MAX_WORKERS = 20
PER_DOMAIN_LIMIT = 3
STALE_WARN_DAYS = 7
STALE_ALARM_DAYS = 30
# Bulk sources are refreshed externally; staleness rules don't apply the same
# way (OpenSanctions updates every few days, ICIJ is a static leak archive).
BULK_SOURCE_PREFIXES = ("opensanctions_", "icij_", "fatf_")
LAYOUT_DRIFT_PCT = 0.10   # 10% change in element count flags LAYOUT_CHANGED
ANOMALY_DROP_PCT = 50.0   # row count -50% triggers DATA_ZEROED severity

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; OverwatchAML-Monitor/2.0; "
    "+https://resurgentindia.com)"
)

# CSRF / dynamic-chrome patterns to strip before hashing. Borrowed from
# utils/change_detector.py — these false-positive on CBI/NIA pages otherwise.
DYNAMIC_PATTERNS = [
    re.compile(r'<meta\s+name=["\']csrf-token["\']\s+content=["\'][^"\']*["\']\s*/?>',
               re.IGNORECASE),
    re.compile(r'<input[^>]+name=["\'](?:_token|csrf_token|authenticity_token)["\'][^>]*>',
               re.IGNORECASE),
    # Common timestamps in <time> tags or data-attrs
    re.compile(r'<time[^>]*>[^<]+</time>', re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# env

def load_env() -> dict:
    env = dict(os.environ)
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


ENV = load_env()


# ---------------------------------------------------------------------------
# Logging

VERBOSE = False


def log(msg: str, level: str = "INFO") -> None:
    if level == "DEBUG" and not VERBOSE:
        return
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level:<5} {msg}", flush=True)


# ---------------------------------------------------------------------------
# IO helpers

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log(f"could not read {path}: {e}", "WARN")
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def load_sources() -> list[dict]:
    with open(SOURCES_PATH) as f:
        return json.load(f).get("sources", [])


# ---------------------------------------------------------------------------
# DB

def db_state() -> tuple[dict, dict]:
    """Return ({source_id: row_count}, {source_id: last_scraped_iso}). Queries
    watchlist_records directly rather than the source_health snapshot — the
    snapshot can be days old (monitor_sources.py runs separately) and we want
    today's true state, not yesterday's projection."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=ENV.get("PG_HOST", "localhost"),
            user=ENV.get("PG_USER", "aayush"),
            password=ENV.get("PG_PASSWORD", "aayush123"),
            dbname=ENV.get("PG_DB", "risk_pipeline"),
            connect_timeout=10,
        )
    except Exception as e:
        log(f"DB connect failed ({e}) — proceeding without DB checks", "WARN")
        return {}, {}
    counts: dict = {}
    last_scraped: dict = {}
    try:
        with conn.cursor() as cur:
            # scraped_at is TEXT (mixed ISO formats), so we just MAX it as a
            # string and parse in Python. The GROUP BY scan over 6M rows takes
            # ~4s on local and similar on RDS — acceptable in a once-a-day job.
            cur.execute("""
                SELECT source_id, COUNT(*), MAX(scraped_at)
                FROM watchlist_records
                WHERE source_id IS NOT NULL AND source_id <> ''
                GROUP BY source_id
            """)
            for sid, n, ls in cur.fetchall():
                counts[sid] = int(n)
                if ls:
                    last_scraped[sid] = str(ls)
    except Exception as e:
        log(f"watchlist_records aggregate query failed: {e}", "WARN")
    finally:
        conn.close()
    return counts, last_scraped


def _parse_scraped_at(s: str) -> Optional[dt.datetime]:
    """scraped_at is TEXT and ranges over a few formats — '2026-05-14 15:27:29',
    '2026-05-20T06:54:27.036021+00:00', etc. Strip tz info and parse loosely."""
    if not s:
        return None
    s = s.strip()
    # Drop timezone for naive comparison
    for tz_marker in ("+00:00", "+0000", "Z"):
        if s.endswith(tz_marker):
            s = s[: -len(tz_marker)]
            break
    # Some have trailing microseconds with offset, e.g. "2026-05-20T06:54:27.036021+00:00"
    s = re.sub(r"([+\-]\d{2}:?\d{2})$", "", s)
    s = s.replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def previous_counts() -> dict:
    """Per-source counts from before today's scrape, written by run_all.sh."""
    return {k: int(v) for k, v in load_json(
        os.path.join(LOG_DIR, "pre_scrape_counts.json"), {}).items()}


# ---------------------------------------------------------------------------
# HTTP layer with per-domain throttle

class DomainThrottle:
    """Cap concurrent in-flight requests per domain so we don't hammer a host
    just because 20 of its pages happen to be queued together."""

    def __init__(self, per_domain_limit: int):
        self.limit = per_domain_limit
        self._lock = threading.Lock()
        self._sema: dict[str, threading.Semaphore] = {}

    def _get(self, domain: str) -> threading.Semaphore:
        with self._lock:
            s = self._sema.get(domain)
            if s is None:
                s = threading.Semaphore(self.limit)
                self._sema[domain] = s
            return s

    def acquire(self, url: str):
        host = urllib.parse.urlparse(url).netloc.lower()
        return _ThrottleCtx(self._get(host))


class _ThrottleCtx:
    def __init__(self, sem):
        self.sem = sem

    def __enter__(self):
        self.sem.acquire()

    def __exit__(self, *exc):
        self.sem.release()


_throttle = DomainThrottle(PER_DOMAIN_LIMIT)


def http_head(url: str) -> dict:
    """HEAD with redirect-following. Returns
    {status, elapsed_ms, headers (subset), redirect_to, error}."""
    import requests
    out = {"status": None, "elapsed_ms": None, "redirect_to": None,
           "headers": {}, "error": None}
    if not url:
        out["error"] = "no_url"
        return out
    t0 = time.perf_counter()
    try:
        with _throttle.acquire(url):
            # Some hosts (Cloudflare) 405 HEAD; fall back to a tiny GET.
            r = requests.head(url, timeout=HEAD_TIMEOUT,
                              allow_redirects=True,
                              headers={"User-Agent": DEFAULT_UA})
            if r.status_code in (403, 405, 501):
                r = requests.get(url, timeout=HEAD_TIMEOUT,
                                 allow_redirects=True, stream=True,
                                 headers={"User-Agent": DEFAULT_UA})
                # We never read the body — just the status.
                r.close()
        out["status"] = r.status_code
        out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        for h in ("content-type", "content-length", "last-modified",
                  "server", "cf-ray", "x-cache"):
            if h in r.headers:
                out["headers"][h] = r.headers[h][:200]
        if r.url != url:
            out["redirect_to"] = r.url
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return out


def http_get_text(url: str) -> tuple[Optional[str], Optional[str]]:
    """GET with text decoding. Returns (text, error). Caps response at 5MB so
    a misconfigured source (e.g. a 200MB binary served on the wrong URL)
    doesn't blow up monitor memory."""
    import requests
    if not url:
        return None, "no_url"
    try:
        with _throttle.acquire(url):
            r = requests.get(url, timeout=GET_TIMEOUT,
                             allow_redirects=True, stream=True,
                             headers={"User-Agent": DEFAULT_UA})
            if r.status_code != 200:
                r.close()
                return None, f"http_{r.status_code}"
            chunks = []
            total = 0
            for chunk in r.iter_content(64 * 1024, decode_unicode=False):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= 5 * 1024 * 1024:
                    break
            r.close()
        raw = b"".join(chunks)
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
        return text, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def _strip_dynamic(text: str) -> str:
    for pat in DYNAMIC_PATTERNS:
        text = pat.sub("", text)
    return text


def _content_hash(text: str) -> str:
    return hashlib.sha256(_strip_dynamic(text).encode("utf-8",
                                                       errors="replace")).hexdigest()


def _selector_fingerprint(text: str, selector: str) -> Optional[dict]:
    """Parse with lxml, count selector hits + hash the joined HTML of those
    nodes. Returns None on parse failure or zero matches."""
    try:
        from lxml import html as lxml_html
        from lxml.cssselect import CSSSelector
    except Exception:
        return None
    try:
        doc = lxml_html.fromstring(text)
        sel = CSSSelector(selector)
        nodes = sel(doc)
    except Exception:
        return None
    if not nodes:
        return {"count": 0, "hash": None}
    h = hashlib.sha256()
    for n in nodes:
        try:
            h.update(lxml_html.tostring(n))
        except Exception:
            continue
    return {"count": len(nodes), "hash": h.hexdigest()}


# ---------------------------------------------------------------------------
# Per-source check

def classify_http(http: dict) -> tuple[str, str]:
    """Return (severity, code) given a HEAD result.
       severity in {OK, WARN, CRIT}, code in {HTTP_OK, HTTP_REDIRECT,
       HTTP_BLOCKED, HTTP_DOWN, HTTP_ERROR, NO_URL}."""
    if http.get("error") == "no_url":
        return "WARN", "NO_URL"
    if http.get("error"):
        err = http["error"]
        if "Timeout" in err or "ConnectTimeout" in err:
            return "CRIT", "HTTP_DOWN"
        if "ConnectionError" in err or "NameResolutionError" in err:
            return "CRIT", "HTTP_DOWN"
        if "SSLError" in err:
            return "WARN", "HTTP_ERROR"
        return "WARN", "HTTP_ERROR"
    status = http.get("status")
    if status is None:
        return "WARN", "HTTP_ERROR"
    if 200 <= status < 300:
        return "OK", "HTTP_OK"
    if 300 <= status < 400:
        return "INFO", "HTTP_REDIRECT"
    if status in (403, 401, 429, 451):
        return "WARN", "HTTP_BLOCKED"
    if status == 404:
        return "CRIT", "HTTP_DOWN"
    if 500 <= status < 600:
        return "CRIT", "HTTP_DOWN"
    return "WARN", "HTTP_ERROR"


def check_one(src: dict, state: dict, skip_http: bool,
              skip_content: bool) -> dict:
    """Run all checks for one source. Mutates state['content_hashes'] and
    state['layout_fingerprints'] when a fresh value is computed."""
    sid = src.get("id")
    url = src.get("url")
    selector = src.get("change_detection_selector")
    cd_enabled = bool(src.get("change_detection"))
    stype = src.get("type")

    result = {
        "source_id": sid,
        "agency": src.get("agency"),
        "list_name": src.get("list_name"),
        "url": url,
        "type": stype,
        "status_config": src.get("status"),
        "alerts": [],  # list of {type, severity, message, ...}
        "http": None,
        "content_hash": None,
        "layout": None,
    }

    if not url or src.get("status") in ("url_not_found", "dead_url"):
        # Nothing actionable to check — skip silently.
        return result

    # ---- HTTP ----
    if not skip_http:
        http = http_head(url)
        result["http"] = http
        sev, code = classify_http(http)
        if code != "HTTP_OK":
            msg_bits = [f"HTTP {http.get('status') or 'ERR'}"]
            if http.get("error"):
                msg_bits.append(http["error"])
            if http.get("redirect_to") and http.get("redirect_to") != url:
                msg_bits.append(f"redirected to {http['redirect_to']}")
            result["alerts"].append({
                "type": code,
                "severity": sev,
                "message": " · ".join(msg_bits),
                "url": url,
            })

    # ---- Content hash + layout (only on change_detection sources to
    # keep the run fast — 401 GETs is enough; we don't fetch the world). ----
    should_fetch = (cd_enabled and not skip_content
                    and stype in ("html", "config", "playwright", "js")
                    and result["http"] is not None
                    and (result["http"].get("status") or 0) == 200)
    if should_fetch:
        text, err = http_get_text(url)
        if text is not None:
            h = _content_hash(text)
            result["content_hash"] = h
            prev_h = state["content_hashes"].get(sid)
            if prev_h and prev_h != h:
                result["alerts"].append({
                    "type": "CONTENT_CHANGED",
                    "severity": "INFO",
                    "message": (f"Page content hash changed "
                                f"(was {prev_h[:10]}, now {h[:10]})."),
                    "url": url,
                })
            state["content_hashes"][sid] = h

            if selector:
                fp = _selector_fingerprint(text, selector)
                if fp is not None:
                    result["layout"] = fp
                    prev_fp = state["layout_fingerprints"].get(sid) or {}
                    prev_count = prev_fp.get("count")
                    if prev_count is not None and prev_count > 0:
                        delta = fp["count"] - prev_count
                        pct = abs(delta) / max(1, prev_count)
                        if pct >= LAYOUT_DRIFT_PCT or fp["count"] == 0:
                            result["alerts"].append({
                                "type": "LAYOUT_CHANGED",
                                "severity": "WARN" if pct < 0.5 else "CRIT",
                                "message": (f"Element count for selector "
                                            f"{selector!r} changed: "
                                            f"{prev_count} → {fp['count']} "
                                            f"({delta:+d})"),
                                "selector": selector,
                                "url": url,
                            })
                    state["layout_fingerprints"][sid] = fp
        elif err and not skip_http:
            # Don't double-report HTTP errors we already flagged in HEAD.
            log(f"GET failed for {sid}: {err}", "DEBUG")

    return result


# ---------------------------------------------------------------------------
# Aggregate checks (run once, not per-source-in-thread)

def check_row_counts(db_counts: dict, pre_counts: dict,
                     sources_by_id: dict) -> list[dict]:
    """Compare current source_health counts against pre_scrape counts. Emit
    DATA_ADDED / DATA_REMOVED / DATA_ZEROED alerts. Also use
    expected_min_records from sources.json as a sanity floor."""
    alerts = []
    seen = set()
    for sid, post in db_counts.items():
        seen.add(sid)
        pre = pre_counts.get(sid)
        src = sources_by_id.get(sid, {})
        if pre is not None and pre > 0 and post == 0:
            alerts.append({
                "source_id": sid, "type": "DATA_ZEROED", "severity": "CRIT",
                "message": f"Row count: {pre:,} → 0 (scraper likely broke)",
                "delta": -pre, "pre": pre, "post": 0,
            })
            continue
        if pre is not None:
            delta = post - pre
            if delta > 0:
                alerts.append({
                    "source_id": sid, "type": "DATA_ADDED", "severity": "INFO",
                    "message": f"Row count: {pre:,} → {post:,} (+{delta:,})",
                    "delta": delta, "pre": pre, "post": post,
                })
            elif delta < 0:
                drop_pct = abs(delta) / max(1, pre) * 100
                sev = "CRIT" if drop_pct >= ANOMALY_DROP_PCT else "WARN"
                alerts.append({
                    "source_id": sid, "type": "DATA_REMOVED", "severity": sev,
                    "message": (f"Row count: {pre:,} → {post:,} "
                                f"({delta:,}, -{drop_pct:.1f}%)"),
                    "delta": delta, "pre": pre, "post": post,
                })
        floor = src.get("expected_min_records")
        if isinstance(floor, int) and floor > 0 and 0 < post < floor:
            # Avoid double-firing if we already flagged a removal.
            already = any(a for a in alerts
                          if a["source_id"] == sid and a["type"] == "DATA_REMOVED")
            if not already:
                alerts.append({
                    "source_id": sid, "type": "BELOW_MIN_RECORDS",
                    "severity": "WARN",
                    "message": (f"Row count {post:,} below expected minimum "
                                f"{floor:,}"),
                    "post": post, "expected_min": floor,
                })
    # Sources missing entirely from today's snapshot that were present yesterday.
    for sid, pre in pre_counts.items():
        if sid in seen or pre <= 0:
            continue
        alerts.append({
            "source_id": sid, "type": "MISSING_FROM_SNAPSHOT", "severity": "WARN",
            "message": f"Source disappeared from today's snapshot (was {pre:,})",
            "pre": pre,
        })
    return alerts


def check_staleness(last_scraped: dict, sources_by_id: dict) -> list[dict]:
    """Emit STALE / VERY_STALE alerts for sources whose most recent scrape is
    > N days old. Bulk sources (OpenSanctions, ICIJ, FATF) get a looser
    threshold because they're not refreshed by daily scrapers."""
    now = dt.datetime.now()
    alerts = []
    for sid, iso in last_scraped.items():
        ts = _parse_scraped_at(iso)
        if ts is None:
            continue
        age = (now - ts).days
        is_bulk = sid.startswith(BULK_SOURCE_PREFIXES)
        warn = STALE_WARN_DAYS * (3 if is_bulk else 1)
        alarm = STALE_ALARM_DAYS * (3 if is_bulk else 1)
        if age >= alarm:
            alerts.append({
                "source_id": sid, "type": "VERY_STALE", "severity": "CRIT",
                "message": f"Not scraped in {age} days (alarm threshold {alarm}d)",
                "age_days": age,
            })
        elif age >= warn:
            alerts.append({
                "source_id": sid, "type": "STALE", "severity": "WARN",
                "message": f"Not scraped in {age} days (warn threshold {warn}d)",
                "age_days": age,
            })
    return alerts


def check_opensanctions_freshness() -> list[dict]:
    """If data/opensanctions_*.csv files exist, check the max `last_change`
    field — alert if older than 48h (PEPs/Crime/Sanctions update daily-ish)."""
    alerts = []
    for fname, threshold_h in (
            ("opensanctions_peps.csv", 48),
            ("opensanctions_debarment.csv", 96),
            ("opensanctions_crime.csv", 48)):
        path = os.path.join(DATA_DIR, fname)
        if not os.path.exists(path):
            continue
        sid = fname.replace(".csv", "")
        try:
            max_change = None
            with open(path, encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    lc = (row.get("last_change") or "").strip()
                    if not lc:
                        continue
                    if max_change is None or lc > max_change:
                        max_change = lc
            if max_change:
                try:
                    ts = dt.datetime.fromisoformat(max_change.split("+")[0]
                                                    .replace("Z", "")
                                                    .strip())
                    age_h = (dt.datetime.now() - ts).total_seconds() / 3600
                    if age_h > threshold_h:
                        alerts.append({
                            "source_id": sid,
                            "type": "BULK_STALE", "severity": "WARN",
                            "message": (f"OpenSanctions last_change {max_change} "
                                        f"is {age_h:.0f}h old "
                                        f"(threshold {threshold_h}h)"),
                            "age_hours": int(age_h),
                        })
                except Exception:
                    pass
        except Exception as e:
            log(f"OS freshness check failed for {fname}: {e}", "DEBUG")
    return alerts


def check_fatf_changes() -> list[dict]:
    """Compare today's fatf_lists.csv against logs/fatf_previous.json. Same
    diff that scripts/compare_counts.py computes — surface it here too so
    the monitor JSON is the one place to read for any source change."""
    alerts = []
    csv_path = os.path.join(DATA_DIR, "fatf_lists.csv")
    prev_path = os.path.join(LOG_DIR, "fatf_previous.json")
    if not os.path.exists(csv_path):
        return alerts
    today = {"black": set(), "grey": set()}
    try:
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lst = (row.get("source_list") or "").lower()
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                if "black" in lst:
                    today["black"].add(name)
                elif "grey" in lst or "monitoring" in lst:
                    today["grey"].add(name)
    except Exception as e:
        log(f"FATF CSV parse: {e}", "WARN")
        return alerts
    prev = load_json(prev_path, {})
    prev_black = set(prev.get("black", []))
    prev_grey = set(prev.get("grey", []))
    diffs = []
    for kind, cur, old in (("black", today["black"], prev_black),
                            ("grey", today["grey"], prev_grey)):
        added = sorted(cur - old)
        removed = sorted(old - cur)
        if added or removed:
            diffs.append((kind, added, removed))
    if diffs:
        bits = []
        for kind, a, r in diffs:
            if a:
                bits.append(f"{kind} added: {', '.join(a)}")
            if r:
                bits.append(f"{kind} removed: {', '.join(r)}")
        alerts.append({
            "source_id": "fatf_lists",
            "type": "FATF_LIST_CHANGED", "severity": "WARN",
            "message": "; ".join(bits),
        })
    return alerts


# ---------------------------------------------------------------------------
# Slack

def post_slack(report: dict) -> None:
    webhook = ENV.get("SLACK_WEBHOOK_URL", "")
    if not webhook:
        log("no SLACK_WEBHOOK_URL — skipping Slack post", "WARN")
        return
    summary = report["summary"]
    alerts = report["alerts"]
    high = [a for a in alerts if a["severity"] in ("CRIT", "WARN")]
    if not high and summary.get("content_changed", 0) == 0 \
            and summary.get("data_added", 0) == 0:
        log("nothing alert-worthy — skipping Slack", "INFO")
        return

    header_text = "🔔 Overwatch AML — Source Monitor"
    lines: list[str] = []
    lines.append(
        f"Checked *{report['total_sources_checked']}* sources "
        f"(*{summary.get('http_checks', 0)}* HTTP probes, "
        f"*{summary.get('healthy', 0)}* healthy)"
    )
    if summary.get("data_added"):
        lines.append(f":chart_with_upwards_trend: *Data added:* "
                     f"{summary['data_added']} source(s)")
    if summary.get("data_removed"):
        lines.append(f":chart_with_downwards_trend: *Data removed:* "
                     f"{summary['data_removed']} source(s)")
    if summary.get("data_zeroed"):
        lines.append(f":rotating_light: *Sources zeroed:* "
                     f"{summary['data_zeroed']} source(s) — likely broken")
    if summary.get("content_changed"):
        lines.append(f":pencil2: *Content changed:* "
                     f"{summary['content_changed']} page(s) updated upstream")
    if summary.get("layout_changed"):
        lines.append(f":warning: *Layout changed:* "
                     f"{summary['layout_changed']} source(s) — may need scraper update")
    if summary.get("http_down"):
        lines.append(f":x: *HTTP down:* {summary['http_down']} source(s)")
    if summary.get("http_blocked"):
        lines.append(f":lock: *HTTP blocked/403:* "
                     f"{summary['http_blocked']} source(s)")
    if summary.get("very_stale"):
        lines.append(f":hourglass: *Very stale (30d+):* "
                     f"{summary['very_stale']} source(s)")
    if summary.get("fatf_changed"):
        lines.append(":globe_with_meridians: *FATF list changed* — see report")

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": header_text}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "\n".join(lines) or "no changes"}},
    ]

    # Surface top 6 critical/warning alerts in detail.
    crit = [a for a in alerts if a["severity"] == "CRIT"][:6]
    if crit:
        body = "\n".join(
            f"• `{a.get('source_id') or '-'}` *{a['type']}*: {a['message'][:160]}"
            for a in crit
        )
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": "*Critical alerts:*\n" + body}})

    payload = {"blocks": blocks, "text": "Overwatch AML monitor report"}
    try:
        import urllib.request
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = (resp.status == 200)
            log(f"Slack post: http={resp.status} ok={ok}", "INFO")
    except Exception as e:
        log(f"Slack post failed: {e}", "WARN")


# ---------------------------------------------------------------------------
# Orchestrator

def run(args) -> dict:
    sources = load_sources()
    if args.source:
        sources = [s for s in sources if s.get("id") == args.source]
        if not sources:
            log(f"no source matches id={args.source}", "WARN")
            return {}
    sources_by_id = {s.get("id"): s for s in sources if s.get("id")}

    state = {
        "content_hashes": load_json(HASHES_PATH, {}),
        "layout_fingerprints": load_json(LAYOUT_PATH, {}),
    }

    db_counts, last_scraped = ({}, {}) if args.skip_db else db_state()
    pre = previous_counts()

    # If we're debugging one source, drop the global aggregates that have
    # nothing to do with it — otherwise the report drowns the user in
    # unrelated stale/missing alerts.
    if args.source:
        db_counts = {k: v for k, v in db_counts.items() if k == args.source}
        last_scraped = {k: v for k, v in last_scraped.items() if k == args.source}
        pre = {k: v for k, v in pre.items() if k == args.source}

    log(f"loaded {len(sources)} sources "
        f"({len(db_counts)} with DB data, {len(pre)} with pre-counts)")

    # Per-source HTTP/content/layout check, in a thread pool.
    per_source: list[dict] = []
    targets = [s for s in sources if s.get("url")]
    if args.data_only:
        log(f"--data-only: skipping HTTP probes for {len(targets)} URL sources")
        per_source = [check_one(s, state, skip_http=True, skip_content=True)
                      for s in targets]
    else:
        skip_content = args.no_content
        log(f"running HTTP+content checks on {len(targets)} sources "
            f"with {MAX_WORKERS} workers (per-domain limit={PER_DOMAIN_LIMIT})")
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(check_one, s, state, False, skip_content): s
                    for s in targets}
            done = 0
            for fut in as_completed(futs):
                try:
                    per_source.append(fut.result())
                except Exception as e:
                    src = futs[fut]
                    log(f"check failed for {src.get('id')}: {e}", "WARN")
                done += 1
                if done % 100 == 0:
                    log(f"  progress: {done}/{len(targets)} "
                        f"({time.perf_counter()-t0:.1f}s)", "DEBUG")
        log(f"HTTP/content phase done in {time.perf_counter()-t0:.1f}s")

    # Aggregate alerts.
    row_alerts = check_row_counts(db_counts, pre, sources_by_id) if db_counts \
        else []
    stale_alerts = check_staleness(last_scraped, sources_by_id) \
        if last_scraped else []
    bulk_alerts = check_opensanctions_freshness()
    fatf_alerts = check_fatf_changes()

    # Flatten per-source alerts back into the global list.
    flat_alerts: list[dict] = []
    for ps in per_source:
        for a in ps.get("alerts", []):
            flat_alerts.append({
                "source_id": ps["source_id"],
                "agency": ps.get("agency"),
                "list_name": ps.get("list_name"),
                **a,
            })
    flat_alerts.extend(row_alerts)
    flat_alerts.extend(stale_alerts)
    flat_alerts.extend(bulk_alerts)
    flat_alerts.extend(fatf_alerts)

    # Summary counts.
    by_type: dict = {}
    for a in flat_alerts:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    http_probed = sum(1 for ps in per_source if ps.get("http") is not None)
    http_ok = sum(1 for ps in per_source
                  if ps.get("http") and (ps["http"].get("status") or 0) // 100 == 2)
    summary = {
        "content_changed": by_type.get("CONTENT_CHANGED", 0),
        "layout_changed": by_type.get("LAYOUT_CHANGED", 0),
        "data_added": by_type.get("DATA_ADDED", 0),
        "data_removed": by_type.get("DATA_REMOVED", 0),
        "data_zeroed": by_type.get("DATA_ZEROED", 0),
        "below_min_records": by_type.get("BELOW_MIN_RECORDS", 0),
        "missing_from_snapshot": by_type.get("MISSING_FROM_SNAPSHOT", 0),
        "http_down": by_type.get("HTTP_DOWN", 0),
        "http_blocked": by_type.get("HTTP_BLOCKED", 0),
        "http_redirect": by_type.get("HTTP_REDIRECT", 0),
        "http_error": by_type.get("HTTP_ERROR", 0),
        "stale": by_type.get("STALE", 0),
        "very_stale": by_type.get("VERY_STALE", 0),
        "bulk_stale": by_type.get("BULK_STALE", 0),
        "fatf_changed": by_type.get("FATF_LIST_CHANGED", 0),
        "http_checks": http_probed,
        "healthy": http_ok,
    }

    report = {
        "timestamp": dt.datetime.now(dt.timezone.utc)
                      .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "total_sources_checked": len(sources),
        "total_with_url": len([s for s in sources if s.get("url")]),
        "alerts": flat_alerts,
        "summary": summary,
        "per_source_count": len(per_source),
        "args": {
            "data_only": args.data_only,
            "no_content": args.no_content,
            "single_source": args.source,
        },
    }

    # Persist state and report.
    save_json(HASHES_PATH, state["content_hashes"])
    save_json(LAYOUT_PATH, state["layout_fingerprints"])
    save_json(REPORT_PATH, report)
    log(f"wrote {REPORT_PATH} ({len(flat_alerts)} alerts)")

    # Console summary.
    print("=" * 70)
    print(f"Source Monitor v2 — {report['timestamp']}")
    print("=" * 70)
    print(f"Sources scanned       : {report['total_sources_checked']}")
    print(f"With URL              : {report['total_with_url']}")
    print(f"HTTP probes           : {summary['http_checks']}")
    print(f"Healthy (HTTP 2xx)    : {summary['healthy']}")
    print()
    print("ALERTS")
    print("-" * 70)
    for k in ("data_zeroed", "data_added", "data_removed", "content_changed",
              "layout_changed", "http_down", "http_blocked", "very_stale",
              "stale", "below_min_records", "missing_from_snapshot",
              "bulk_stale", "fatf_changed"):
        v = summary.get(k, 0)
        if v:
            print(f"  {k:<26} {v}")
    if not any(summary.get(k) for k in summary if k not in ("http_checks", "healthy")):
        print("  no alerts")

    # Optional Slack.
    if args.slack and not args.dry_run:
        post_slack(report)
    elif args.dry_run:
        log("--dry-run set: not posting Slack", "INFO")

    return report


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="full run (default unless --source given)")
    ap.add_argument("--data-only", action="store_true",
                    help="skip HTTP/content checks; only DB + CSV freshness")
    ap.add_argument("--no-content", action="store_true",
                    help="run HTTP HEAD but skip content+layout GETs")
    ap.add_argument("--source", help="check one source id and print result")
    ap.add_argument("--skip-db", action="store_true",
                    help="skip source_health/pre_scrape DB checks")
    ap.add_argument("--slack", action="store_true",
                    help="post a Slack alert if any alert was raised")
    ap.add_argument("--dry-run", action="store_true",
                    help="do not post Slack, just print")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose
    if not (args.all or args.data_only or args.source):
        ap.error("pick --all, --data-only, or --source <id>")
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
