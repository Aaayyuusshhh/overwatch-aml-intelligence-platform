"""Probe NSE regulations page via Playwright with cookie warmup."""
import re
from playwright.sync_api import sync_playwright

URL = "https://www.nseindia.com/regulations/exchange-market-surveillance-regulatory-actions"

with sync_playwright() as pw:
    br = pw.chromium.launch(headless=True)
    ctx = br.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        ignore_https_errors=True,
        # NSE serves HTTP/2 RST_STREAM on direct deep links from headless;
        # disable HTTP/2 by setting an HTTP/1.1-only Accept header isn't
        # enough. Workaround: hit the homepage first to set cookies, then
        # navigate to the regulations page.
    )
    pg = ctx.new_page()
    print("warmup: nseindia.com homepage")
    try:
        pg.goto("https://www.nseindia.com/", wait_until="domcontentloaded",
                timeout=45_000)
        pg.wait_for_timeout(5000)
        print(f"  homepage cookies: {len(ctx.cookies())}")
    except Exception as e:
        print(f"  homepage err: {type(e).__name__}: {str(e)[:160]}")
    print(f"\nnow goto {URL}")
    try:
        pg.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print(f"  goto err: {type(e).__name__}: {str(e)[:200]}")
        br.close()
        raise SystemExit(0)
    pg.wait_for_timeout(15_000)
    html = pg.content()
    print(f"len={len(html)}")

    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", html, re.I)
    print(f"\ntables: {len(tables)}")
    for ti, t in enumerate(tables[:8]):
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
        if not trs: continue
        cells0 = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", trs[0], re.I)
        clean = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in cells0]
        print(f"  table[{ti}] rows={len(trs)} hdr={clean[:7]}")
        if len(trs) > 1:
            cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", trs[1], re.I)
            sample = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()[:50]
                      for c in cells]
            print(f"    sample: {sample[:7]}")

    print("\nkeyword anchors:")
    seen = set()
    for m in re.finditer(
        r'''<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]{0,300}?)</a>''',
        html, re.I):
        href, raw = m.group(1), m.group(2)
        txt = re.sub(r"<[^>]+>", " ", raw)
        txt = re.sub(r"\s+", " ", txt).strip()
        blob = (href + " " + txt).lower()
        if any(k in blob for k in ("networth", "inadequate", "authorized person",
                                   "authorised person", "cancelled", "ap cancellation",
                                   "disciplinary")):
            if href in seen: continue
            seen.add(href)
            print(f"  {href[:130]}")
            print(f"      [{txt[:100]}]")
    br.close()
