"""Probe ESIC homepage + key paths to find a defaulter list URL that works."""
import re
import sys
from scrapling import Fetcher

CANDIDATES = [
    "https://www.esic.gov.in/",
    "https://www.esic.gov.in/defaulter-employer-list",
    "https://www.esic.gov.in/employer-defaulter-list",
    "https://www.esic.gov.in/employer/defaulter-list",
    "https://www.esic.gov.in/employer-defaulters",
    "https://www.esic.in/defaulter-list",
    "https://www.esic.gov.in/recovery",
]

for url in CANDIDATES:
    try:
        r = Fetcher.get(url, timeout=30, retries=1, retry_delay=0, verify=False)
    except Exception as e:
        print(f"{url}: {type(e).__name__}: {str(e)[:120]}")
        continue
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    status = getattr(r, "status", None)
    print(f"\n=== {url} -> {status} len={len(body)} ===")
    # Search for any anchor mentioning 'default'
    n = 0
    for m in re.finditer(
            r'''<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]{0,200}?)</a>''',
            body, re.I):
        href, raw = m.group(1), m.group(2)
        txt = re.sub(r"<[^>]+>", " ", raw)
        txt = re.sub(r"\s+", " ", txt).strip()
        blob = (href + " " + txt).lower()
        if "default" in blob or "recovery" in blob:
            print(f"  {href}  | {txt[:100]}")
            n += 1
            if n > 30:
                break
