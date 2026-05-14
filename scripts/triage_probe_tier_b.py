"""Lightweight static-fetch probe of every Tier B partial URL. No
scraping or file modification — just records:
  - HTTP status
  - body length
  - number of <table> elements (any size)
  - largest table's data-row count
  - number of AML-keyword PDF/XLSX/CSV anchors
  - JS-framework markers in body

Output: prints a compact table sorted by 'how recoverable does this look'.
"""
import json
import os
import re
import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H  = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

AML_KW = ("defaulter", "blacklist", "blacklisted", "debarred", "banned",
          "suspended", "wanted", "penalty", "wilful", "expelled", "watchlist",
          "offender", "convicted", "proclaimed", "fugitive", "absconder",
          "disqualified", "cancelled")
JS_MARK = ("ng-app", "ng-version", "v-app", "data-reactroot", "<noscript",
           "please enable javascript", "javascript is required",
           "/static/js/main.", "window.angular")


def probe(s, url):
    try:
        r = s.get(url, timeout=20, verify=False, headers=H,
                  allow_redirects=True)
    except Exception as e:
        return {"err": f"{type(e).__name__}: {str(e)[:80]}",
                "status": None, "len": 0, "tables": 0, "max_rows": 0,
                "aml_files": 0, "js_marks": [], "redirected": False}
    text = r.text or ""
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", text, re.I)
    max_rows = 0
    for t in tables:
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
        max_rows = max(max_rows, len(trs))
    # AML-keyword files
    aml_files = 0
    seen = set()
    for m in re.finditer(
            r'''href=["']([^"']+\.(?:pdf|xlsx?|csv|docx?))[^"']*["']''',
            text, re.I):
        href = m.group(1).lower()
        if any(k in href for k in AML_KW) and href not in seen:
            seen.add(href)
            aml_files += 1
    js_marks = [m for m in JS_MARK if m in text.lower()]
    return {
        "status": r.status_code,
        "len": len(text),
        "tables": len(tables),
        "max_rows": max_rows,
        "aml_files": aml_files,
        "js_marks": js_marks[:3],
        "redirected": r.url != url,
        "final_url": r.url,
        "err": "",
    }


def verdict(p):
    """Short label estimating fix effort."""
    if p.get("err"):
        return "ERR — try later"
    if p["status"] and p["status"] >= 400:
        return f"HTTP {p['status']}  — dead/restricted"
    if p["len"] < 1500:
        return "tiny body — likely soft-404"
    if p["aml_files"] > 0 and p["max_rows"] < 3:
        return f"PDF/XLSX present ({p['aml_files']}) — 30 min download+parse"
    if p["max_rows"] >= 3:
        return f"table has {p['max_rows']} rows — 30 min generic re-extract"
    if p["js_marks"]:
        return f"JS shell ({','.join(p['js_marks'])}) — needs Playwright"
    if p["redirected"]:
        return "redirected; likely dead URL"
    return "empty page / chrome only — likely walled"


def main():
    tiers = json.load(open(os.path.join(PROJECT_ROOT, "logs",
                                         "_triage_tiers.json")))
    by_ppt = {p["ppt"]: p for p in tiers["partials"]}
    b_ppts = tiers["B"]

    print(f"### STEP 4 — probing {len(b_ppts)} Tier B URLs\n")
    print(f"{'ppt':>4}  {'http':>4}  {'len':>6}  {'tbl':>3}  {'rows':>4}  "
          f"{'pdf':>3}  verdict")
    print("-" * 130)

    s = requests.Session()
    rows = []
    for ppt in b_ppts:
        meta = by_ppt[ppt]
        url = meta["url"]
        if not url:
            rows.append((ppt, meta, None, "no URL"))
            continue
        p = probe(s, url)
        v = verdict(p)
        rows.append((ppt, meta, p, v))
        print(f"{ppt:>4}  {str(p.get('status','?')):>4}  {p['len']:>6}  "
              f"{p['tables']:>3}  {p['max_rows']:>4}  {p['aml_files']:>3}  "
              f"{v[:80]}")
        time.sleep(0.5)
    s.close()

    # Bucket by verdict shape
    buckets = {}
    for ppt, meta, p, v in rows:
        bucket = v.split(" — ")[0] if " — " in v else v
        buckets.setdefault(bucket, []).append((ppt, meta, v))

    print(f"\n--- bucket summary ---")
    for b, items in sorted(buckets.items(), key=lambda x: -len(x[1])):
        print(f"  [{len(items):>2}]  {b}")
        for ppt, meta, v in items[:6]:
            print(f"        #{ppt:>3}  {meta['agency'][:30]:<30}  "
                  f"{meta['list_name'][:40]:<40}  → {v}")
        if len(items) > 6:
            print(f"        ... +{len(items)-6} more")

    with open(os.path.join(PROJECT_ROOT, "logs",
                           "_triage_probe.json"), "w") as f:
        json.dump([{"ppt": ppt, **(p or {}), "verdict": v}
                   for ppt, _, p, v in rows], f, indent=2, default=str)
    print("wrote: logs/_triage_probe.json")


if __name__ == "__main__":
    main()
