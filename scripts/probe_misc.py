"""Probe several pages: ESIC defaulters, Coal India banning, Jharkhand."""
import re
import sys
from scrapling import Fetcher

def probe(url, label, kw_filter=None):
    print(f"\n=== {label} :: {url} ===")
    try:
        r = Fetcher.get(url, timeout=45, retries=1, retry_delay=0, verify=False)
    except Exception as e:
        print(f"  fetch error: {type(e).__name__}: {str(e)[:160]}")
        return
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    status = getattr(r, "status", None)
    print(f"  status={status}  len={len(body)}")

    # Tables
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", body, re.I)
    print(f"  tables: {len(tables)}")
    for ti, t in enumerate(tables[:3]):
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
        if not trs:
            continue
        cells0 = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", trs[0], re.I)
        clean = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in cells0]
        print(f"    table[{ti}] rows={len(trs)} header={clean[:8]}")

    # PDF links
    pdfs = re.findall(r'''href=["']([^"']+\.pdf[^"']*)["']''', body, re.I)
    print(f"  pdf refs: {len(pdfs)}")
    seen = set()
    for p in pdfs:
        if kw_filter and not any(k in p.lower() for k in kw_filter):
            continue
        if p in seen:
            continue
        seen.add(p)
        print(f"    {p}")
        if len(seen) >= 8:
            break

    # JS markers
    js_indicators = []
    bl = body.lower()
    for m in ("loading", "ng-app", "v-app", "data-react", "<noscript",
             "please enable javascript"):
        if m in bl:
            js_indicators.append(m)
    if js_indicators:
        print(f"  js_markers: {js_indicators}")

    # Brief signpost text near 'defaulter' / 'naxal' / 'wanted'
    for kw in ("defaulter", "naxal", "wanted", "banning"):
        m = re.search(rf"\b{kw}\w*", bl)
        if m:
            i = m.start()
            sn = re.sub(r'<[^>]+>', ' ', body[max(0, i-40):i+200])
            sn = re.sub(r'\s+', ' ', sn).strip()
            print(f"  kw={kw!r} :: {sn[:160]}")
            break


URLS = [
    ("ESIC defaulters",   "https://www.esic.gov.in/defaulters",          None),
    ("ESIC rosro",        "https://www.esic.gov.in/rosro-defaulters",    None),
    ("Coal India banning","https://www.coalindia.in/tenders/banning-order/", None),
    ("Jh rewarded-naxal", "https://jhpolice.gov.in/rewarded-naxal",      None),
    ("Jh most-wanted",    "https://www.jhpolice.gov.in/most-wanted-naxals", None),
    ("Jh wanted",         "https://jhpolice.gov.in/wanted",              None),
]

for label, url, kw in URLS:
    probe(url, label, kw)
