"""
scripts/url_discovery_sweep.py — discover working URLs for sources
currently marked status=url_not_found in sources.json.

Read-only sweep. For each url_not_found source we:

  1. Get the agency's primary domain (reuses classify.AGENCY_DOMAINS).
  2. Synthesize candidate URLs from the list_name + a path library
     of known AML page shapes.
  3. HEAD-probe each candidate (10 s timeout, follow redirects).
  4. On 200, GET the body once and score:
       - HTML body length, content-type
       - presence of AML keywords ("debarred", "blacklist", etc.)
       - count of file-download anchors (.pdf, .xlsx, .xls, .csv, .zip)
  5. Pick the BEST candidate per source by confidence:
       high   = 200 + AML keyword in body
       medium = 200 with no AML keyword (looks generic)
       low    = redirected to homepage or top-level path
       dead   = every candidate failed

Politeness
----------
  - 1.5 s sleep between requests to the SAME domain
  - per-request 10 s timeout
  - cap at MAX_CANDIDATES_PER_SOURCE candidates (default 8)

Output
------
  reports/url_discovery_sweep.csv          — full per-candidate rows
  stdout summary sorted by confidence
"""

import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from urllib.parse import urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from classify import AGENCY_DOMAINS  # reuse the curated map

SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")
REPORTS_DIR  = os.path.join(PROJECT_ROOT, "reports")
REPORT_PATH  = os.path.join(REPORTS_DIR, "url_discovery_sweep.csv")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# Be polite — 1.5 s between requests to the same host.
POLITENESS_SECONDS_PER_HOST = 1.5
TIMEOUT                     = 10
MAX_CANDIDATES_PER_SOURCE   = 8

AML_KEYWORDS = ("debarred", "blacklist", "defaulter", "wanted", "banned",
                "suspended", "penalty", "enforcement", "struck off",
                "cancelled", "wilful", "expelled", "watchlist",
                "offender", "fugitive", "proclaimed", "absconder",
                "disqualified", "convicted")

# Common shared paths to try when the list_name slug doesn't resolve.
COMMON_PATHS = (
    "blacklist", "blacklisted", "debarred", "debarment", "debarred-firms",
    "debarred-entities", "debarred-vendors", "banned", "banned-firms",
    "banning-order", "wanted", "wanted-persons", "wanted-criminals",
    "most-wanted", "defaulters", "defaulter-list", "wilful-defaulters",
    "wilful-defaulter", "suspended", "expelled", "cancelled", "cancelled-list",
    "penalty", "penalties", "orders", "enforcement", "enforcement-orders",
    "struck-off", "circulars", "notifications", "press-release",
    "media/press-release", "vendor-debarment", "list-of-banned-firms",
    "list-of-defaulters",
)

FILE_RE = re.compile(r'''href=["']([^"']+\.(?:pdf|xlsx?|csv|zip|docx?))''', re.I)
TAG_RE  = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# Candidate URL synthesis
# --------------------------------------------------------------------------
def _slugify(text):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text


def _list_name_tokens(list_name):
    """Yield a few useful slugs derived from the list name."""
    if not list_name:
        return []
    full = _slugify(list_name)
    # Common stop-words / fillers
    drops = {"list", "of", "the", "and", "for", "by", "with", "as", "on", "all"}
    words = [w for w in full.split("-") if w not in drops and len(w) > 2]
    out = [full]
    if words:
        out.append("-".join(words))
        out.append("-".join(words[:2]))
        out.append(words[0])
    return list(dict.fromkeys(out))  # dedupe, keep order


def candidates_for(source):
    """Return list[str] of candidate URLs for one source."""
    agency = (source.get("agency") or "").lower().strip()
    domain = AGENCY_DOMAINS.get(agency)
    if not domain:
        return []
    base_https = f"https://{domain}"
    list_slugs = _list_name_tokens(source.get("list_name", ""))

    candidates = []
    # 1) https://<domain>/<list-slug>
    for slug in list_slugs:
        candidates.append(f"{base_https}/{slug}")
    # 2) https://<domain>/<common path>
    for p in COMMON_PATHS:
        candidates.append(f"{base_https}/{p}")
    # 3) https://<domain>/en/<...> for ministries that use language subpaths
    if domain.endswith(".gov.in") or domain.endswith(".nic.in"):
        for slug in list_slugs[:2]:
            candidates.append(f"{base_https}/en/{slug}")
    # 4) https://www.<domain> mirror
    if not domain.startswith("www."):
        candidates.append(f"https://www.{domain}")
        for slug in list_slugs[:2]:
            candidates.append(f"https://www.{domain}/{slug}")

    # Deduplicate, cap.
    seen = set()
    out = []
    for u in candidates:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= MAX_CANDIDATES_PER_SOURCE:
            break
    return out


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------
class HostThrottle:
    """Per-host last-request-time tracker for politeness."""
    def __init__(self, delay):
        self.delay = delay
        self.last  = {}

    def wait(self, url):
        host = urlparse(url).netloc
        now = time.time()
        prev = self.last.get(host, 0)
        gap = now - prev
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self.last[host] = time.time()


def probe(session, url, throttle):
    """Return (head_status, get_status, final_url, content_type, length,
    keywords_found, file_links_count, err)."""
    throttle.wait(url)
    try:
        head = session.head(url, timeout=TIMEOUT, verify=False,
                            allow_redirects=True)
    except Exception as e:
        return None, None, "", "", 0, [], 0, f"{type(e).__name__}: {str(e)[:80]}"

    head_status = head.status_code
    final_url   = head.url
    content_type = head.headers.get("content-type", "")
    length = int(head.headers.get("content-length", 0)) or 0

    if head_status != 200:
        return head_status, None, final_url, content_type, length, [], 0, ""

    # 200 → fetch body
    throttle.wait(url)
    try:
        get = session.get(url, timeout=TIMEOUT, verify=False,
                          allow_redirects=True)
    except Exception as e:
        return head_status, None, final_url, content_type, length, [], 0, \
               f"GET: {type(e).__name__}: {str(e)[:80]}"

    text = get.text or ""
    if not length:
        length = len(text)
    body_lc = text.lower()
    kws = [k for k in AML_KEYWORDS if k in body_lc]
    file_links = len(FILE_RE.findall(text))
    return head_status, get.status_code, get.url, \
           get.headers.get("content-type", content_type), length, \
           kws, file_links, ""


# --------------------------------------------------------------------------
# Confidence scoring
# --------------------------------------------------------------------------
def confidence_of(rows_for_source):
    """rows_for_source: list of per-candidate result dicts. Return
    (best_row, confidence_label). Strategy: pick the best candidate
    by ranking and label the whole source by it."""
    if not rows_for_source:
        return None, "dead"

    def score(r):
        if r["status"] != 200:
            return -3
        if r["kws"]:
            return 3
        # 200 but no AML kws
        # Penalize redirects to root path
        try:
            final_path = urlparse(r["final_url"]).path.strip("/")
        except Exception:
            final_path = ""
        if final_path in ("", "index.html", "home", "en"):
            return 0
        return 1

    best = max(rows_for_source, key=score)
    s = score(best)
    if s >= 3:
        label = "high"
    elif s >= 1:
        label = "medium"
    elif s >= 0:
        label = "low"
    else:
        label = "dead"
    return best, label


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)["sources"]

    targets = [s for s in sources if s.get("status") == "url_not_found"]
    print(f"Sweeping {len(targets)} url_not_found sources…")

    sess = requests.Session()
    sess.headers.update(HEADERS)
    throttle = HostThrottle(POLITENESS_SECONDS_PER_HOST)

    all_rows = []   # one per candidate
    per_source = defaultdict(list)

    for i, s in enumerate(targets, 1):
        ppt = s.get("ppt_number")
        agency = s.get("agency", "")
        list_name = s.get("list_name", "")
        cands = candidates_for(s)
        if not cands:
            print(f"  [{i:>3}/{len(targets)}] #{ppt:>3}  {agency[:34]:<34}  "
                  f"{list_name[:36]:<36}  → no domain in AGENCY_DOMAINS")
            per_source[ppt] = []
            continue

        hits_logged = 0
        for url in cands:
            head_status, get_status, final_url, ct, length, kws, files, err = \
                probe(sess, url, throttle)
            row = {
                "ppt_number":  ppt,
                "agency":      agency,
                "source_list": list_name,
                "url_tried":   url,
                "status_code": head_status if head_status is not None else "",
                "final_url":   final_url,
                "content_type": ct,
                "content_length": length,
                "aml_keywords_found": ",".join(kws),
                "download_links_found": files,
                "err":         err,
                # for confidence scoring
                "status":      head_status,
                "kws":         kws,
            }
            all_rows.append(row)
            per_source[ppt].append(row)
            hits_logged += 1
            # Short-circuit on a strong 200+keywords hit
            if head_status == 200 and kws:
                break

        best, label = confidence_of(per_source[ppt])
        best_url = best["url_tried"] if best else "—"
        kws_str = ",".join(best["kws"]) if best and best["kws"] else ""
        print(f"  [{i:>3}/{len(targets)}] #{ppt:>3}  {agency[:34]:<34}  "
              f"{list_name[:36]:<36}  → {label:<6}  "
              f"({hits_logged} probes)  best={best_url[:60]}"
              f"{'  kws=' + kws_str if kws_str else ''}")

    # Write report
    os.makedirs(REPORTS_DIR, exist_ok=True)
    flds = ["ppt_number", "agency", "source_list", "url_tried",
            "status_code", "final_url", "content_type", "content_length",
            "aml_keywords_found", "download_links_found", "confidence",
            "err"]
    # tag each row with the source's confidence label
    src_label = {}
    for ppt, rows in per_source.items():
        _, label = confidence_of(rows)
        src_label[ppt] = label
    with open(REPORT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flds)
        w.writeheader()
        for r in all_rows:
            w.writerow({
                "ppt_number":  r["ppt_number"],
                "agency":      r["agency"],
                "source_list": r["source_list"],
                "url_tried":   r["url_tried"],
                "status_code": r["status_code"],
                "final_url":   r["final_url"],
                "content_type": r["content_type"],
                "content_length": r["content_length"],
                "aml_keywords_found": r["aml_keywords_found"],
                "download_links_found": r["download_links_found"],
                "confidence":  src_label.get(r["ppt_number"], ""),
                "err":         r["err"],
            })

    # Summary buckets
    bucket = {"high": [], "medium": [], "low": [], "dead": []}
    for ppt, rows in per_source.items():
        best, label = confidence_of(rows)
        s = next((s for s in targets if s.get("ppt_number") == ppt), None)
        bucket[label].append((s, best))

    print(f"\nFull report: {REPORT_PATH}\n")
    for label in ("high", "medium", "low", "dead"):
        items = bucket[label]
        print(f"\n=== {label.upper()} CONFIDENCE ({len(items)}) ===")
        if label == "dead":
            for s, _ in items[:25]:
                print(f"  #{s['ppt_number']:>3}  {s['agency'][:40]:<40}  "
                      f"{s['list_name'][:50]:<50}  all candidates failed")
            if len(items) > 25:
                print(f"  ... +{len(items)-25} more")
            continue
        for s, best in items:
            url = best["url_tried"]
            tail = ""
            if best["kws"]:
                tail = f" | keywords: {','.join(best['kws'])}"
            elif best["status"] == 200:
                tail = f" | len={best['content_length']}"
            elif best["final_url"]:
                tail = f" | → {best['final_url'][:60]}"
            print(f"  #{s['ppt_number']:>3}  {s['agency'][:35]:<35}  "
                  f"{s['list_name'][:35]:<35}\n        URL: {url}{tail}")

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total url_not_found sources: {len(targets)}")
    print(f"  High confidence discoveries: {len(bucket['high'])}")
    print(f"  Medium confidence:           {len(bucket['medium'])}")
    print(f"  Low confidence:              {len(bucket['low'])}")
    print(f"  Still dead:                  {len(bucket['dead'])}")
    sess.close()


if __name__ == "__main__":
    main()
