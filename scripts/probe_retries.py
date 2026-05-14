"""Probe retry candidates with isolated Playwright pages."""
import re
from playwright.sync_api import sync_playwright

KW = ("defaulter", "blacklist", "blacklisted", "debarred", "banned",
      "suspended", "wanted", "penalty", "wilful", "expelled", "watchlist",
      "offender", "convicted", "proclaimed", "fugitive")

CANDIDATES = [
    ("ed_24_red_corner", "https://enforcementdirectorate.gov.in/wanted/red-corner-notices/"),
    ("mse_174_defaulter", "https://msei.in/list-of-defaulter-members"),
    ("mse_175_expelled", "https://msei.in/list-of-expelled-members"),
    ("mcx_180_surrendered", "https://www.mcxindia.com/membership/notice-board/list-of-surrender-members"),
    ("mp_217_wanted", "https://megpolice.gov.in/wanted-person"),
    ("nfra_89_orders", "https://nfra.gov.in/orders"),
    ("rec_156_banned", "https://www.recindia.nic.in/list-of-banned-firms"),
    ("epfo_26_orders", "https://epfindia.gov.in/orders"),
    ("ed_23_press", "https://enforcementdirectorate.gov.in/media/press-release/"),
    ("fiu_28_judg", "https://fiuindia.gov.in/files/Compliance_Orders/orders.html"),
]


def probe_one(ctx, label, url):
    print(f"\n=== {label} :: {url} ===")
    pg = ctx.new_page()
    try:
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            print(f"  goto err: {type(e).__name__}: {str(e)[:140]}")
            return
        pg.wait_for_timeout(10_000)
        html = pg.content()
        print(f"  url-after={pg.url}  len={len(html)}")
        tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", html, re.I)
        good = []
        for t in tables:
            trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
            if len(trs) >= 3:
                hdr = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", trs[0], re.I)
                clean = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in hdr]
                good.append((len(trs), clean[:6]))
        print(f"  tables-with-3+rows: {len(good)}")
        for n, h in good[:3]:
            print(f"    rows={n}  header={h}")
        seen = set()
        for m in re.finditer(r'''href=["']([^"']+\.(?:pdf|xlsx|xls|csv))[^"']*["']''', html, re.I):
            href = m.group(1)
            blob = href.lower()
            if any(k in blob for k in KW) and href not in seen:
                seen.add(href)
        if seen:
            print(f"  AML files: {len(seen)}")
            for h in list(seen)[:6]:
                print(f"    {h[:120]}")
    finally:
        pg.close()


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=True)
    ctx = br.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        ignore_https_errors=True,
    )
    for label, url in CANDIDATES:
        probe_one(ctx, label, url)
    br.close()
