"""Dump every table on /suspended_member with full sample row data."""
import re
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    br = pw.chromium.launch(headless=True)
    ctx = br.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        ignore_https_errors=True,
    )
    pg = ctx.new_page()
    for url in ("https://ncdex.com/suspended_member",
                "https://ncdex.com/investor-services/defaulter-member",
                "https://ncdex.com/investor-services/debarred-entities",
                "https://ncdex.com/existing-members/surrender-of-membership"):
        print(f"\n========= {url} =========")
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"  goto err: {type(e).__name__}: {str(e)[:160]}")
            continue
        pg.wait_for_timeout(10000)
        html = pg.content()
        print(f"  len={len(html)}")

        # Tab-section ids inside the page
        for m in re.finditer(r'<section[^>]+id=["\'](\w+)["\'][^>]*>([\s\S]{0,400}?)<', html, re.I):
            sid, snip = m.group(1), m.group(2)
            txt = re.sub(r"<[^>]+>", " ", snip)
            txt = re.sub(r"\s+", " ", txt).strip()
            print(f"  section id={sid!r} : {txt[:90]}")

        for m in re.finditer(r'<div[^>]+id=["\'](\w+)["\'][^>]*>([\s\S]{0,400}?)<', html, re.I):
            sid, snip = m.group(1), m.group(2)
            if sid.lower() in ("one","two","three","four","five","six","seven","eight","tab1","tab2","tab3","tab4","tab5"):
                txt = re.sub(r"<[^>]+>", " ", snip)
                txt = re.sub(r"\s+", " ", txt).strip()
                print(f"  div id={sid!r} : {txt[:90]}")

        tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", html, re.I)
        print(f"  tables: {len(tables)}")
        for ti, t in enumerate(tables):
            trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
            print(f"    table[{ti}] rows={len(trs)}")
            for ri, tr in enumerate(trs[:3]):
                cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)
                clean = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                         for c in cells]
                print(f"      row[{ri}] = {clean[:8]}")
    br.close()
