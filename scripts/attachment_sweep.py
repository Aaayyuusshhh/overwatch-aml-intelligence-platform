"""
attachment_sweep.py

For each completed source (status=active, no custom scraper), fetch the
URL and report all download links matching AML-relevant keywords. We
deliberately do NOT auto-download — the report is intended for engineer
review. The IOB win (4 XLSX -> 2,605 records) showed file-link mining
adds substantial coverage on top of in-page table extraction.

Usage:
    python -m scripts.attachment_sweep            # report only
    python -m scripts.attachment_sweep --json     # machine-readable

Skips:
- sources with a custom scraper (their authors already know the files)
- sources whose CSV doesn't exist (not yet scraped)
- non-html types (PDFs are themselves the file; JS sources we can't fetch)
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import urljoin

from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")

KEYWORDS = (
    "defaulter", "blacklist", "blacklisted", "debarred", "banned", "banning",
    "suspended", "suspension", "wanted", "penalty", "penalties", "wilful",
    "wilfull", "expelled", "watchlist", "offender", "fraud", "convicted",
    "disqualified", "absconder", "cancelled", "fugitive",
)

FILE_EXTS = (".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx", ".zip")

ANCHOR_RE = re.compile(
    r'''<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]{0,300}?)</a>''', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _is_attachment(href):
    h = href.lower().split("?")[0]
    return h.endswith(FILE_EXTS)


def _anchor_text(raw):
    txt = TAG_RE.sub(" ", raw)
    return re.sub(r"\s+", " ", txt).strip()


def sweep_source(s):
    """Return list of dicts {file_url, link_text, file_type, matched_kw}."""
    url = s.get("url")
    if not url or s.get("type") != "html":
        return None
    if s.get("scraper"):
        return None
    if s.get("status") != "active":
        return None
    sid = s["id"]
    csv_path = os.path.join(DATA_DIR, f"{sid}.csv")
    if not os.path.exists(csv_path):
        return None

    try:
        r = Fetcher.get(url, timeout=30, retries=1, retry_delay=0, verify=False)
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {str(e)[:120]}"}]
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    if getattr(r, "status", None) is None or r.status >= 400:
        return [{"error": f"http_status={getattr(r, 'status', None)}"}]

    out = []
    seen = set()
    for m in ANCHOR_RE.finditer(body):
        href, raw = m.group(1), m.group(2)
        if not _is_attachment(href):
            continue
        txt = _anchor_text(raw)
        blob = (href + " " + txt).lower()
        kws = [k for k in KEYWORDS if k in blob]
        if not kws:
            continue
        full = urljoin(url, href.strip())
        if full in seen:
            continue
        seen.add(full)
        ext = os.path.splitext(full.split("?")[0])[1].lower().lstrip(".")
        out.append({
            "file_url": full,
            "link_text": txt[:140],
            "file_type": ext,
            "matched_kw": ",".join(kws[:3]),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of a table")
    args = ap.parse_args()

    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)["sources"]

    findings = {}
    n_skipped = n_checked = 0
    for s in sources:
        res = sweep_source(s)
        if res is None:
            n_skipped += 1
            continue
        n_checked += 1
        if res:
            findings[s["id"]] = res

    if args.json:
        print(json.dumps({"checked": n_checked, "skipped": n_skipped,
                          "findings": findings}, indent=2))
        return

    print(f"\nChecked: {n_checked} sources    Skipped: {n_skipped}")
    print(f"Sources with attachment matches: {len(findings)}")
    print("=" * 100)
    for sid, items in findings.items():
        print(f"\n[{sid}]")
        for it in items[:8]:
            if "error" in it:
                print(f"  ERROR: {it['error']}")
                continue
            print(f"  ({it['file_type']:5s}) [{it['matched_kw']}]  {it['file_url'][:90]}")
            print(f"           text: {it['link_text']}")
        if len(items) > 8:
            print(f"  ... +{len(items) - 8} more")


if __name__ == "__main__":
    main()
