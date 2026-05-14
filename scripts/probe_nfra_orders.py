"""Probe NFRA orders page via Playwright to design the scraper."""
import re
from playwright.sync_api import sync_playwright

URL = ("https://nfra.gov.in/document/orders-issued-by-nfra-under-section-"
       "1324-of-the-companies-act/")

with sync_playwright() as pw:
    br = pw.chromium.launch(headless=True)
    ctx = br.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        ignore_https_errors=True,
    )
    pg = ctx.new_page()
    print(f"goto {URL}")
    pg.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    pg.wait_for_timeout(10_000)
    html = pg.content()
    print(f"len={len(html)}  url-after={pg.url}\n")

    # Tables
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", html, re.I)
    print(f"tables: {len(tables)}")
    for ti, t in enumerate(tables[:5]):
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
        if not trs: continue
        print(f"  table[{ti}] rows={len(trs)}")
        for ri, tr in enumerate(trs[:5]):
            cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)
            clean = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()[:60]
                     for c in cells]
            print(f"    row {ri}: {clean[:8]}")

    # Structured blocks
    views_row = re.compile(r'''class=["'][^"']*views-row[^"']*["']''', re.I)
    doc_class = re.compile(r'''class=["'][^"']*document[^"']*["']''', re.I)
    print(f"\ndiv.views-row count: {len(views_row.findall(html))}")
    print(f"article tag count: {len(re.findall(r'<article', html, re.I))}")
    print(f"div.document count: {len(doc_class.findall(html))}")
    print("ul/ol classes seen:")
    for m in re.finditer(r'<ul[^>]*class=["\']([^"\']+)["\'][^>]*>', html, re.I):
        print(f"  ul.{m.group(1)[:60]}")

    # PDF links
    pdfs = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)
    print(f"\nPDF anchors: {len(pdfs)}")
    for p in pdfs[:10]:
        print(f"  {p[:130]}")

    # All anchors with date-y text
    print("\nAnchors with date-like text:")
    n = 0
    for m in re.finditer(
        r'''<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]{0,300}?)</a>''', html, re.I):
        href, raw = m.group(1), m.group(2)
        txt = re.sub(r"<[^>]+>", " ", raw); txt = re.sub(r"\s+", " ", txt).strip()
        if re.search(r"\b(?:order|company|matter|m/s|ltd\.?|limited)\b", txt, re.I) and len(txt) > 10:
            print(f"  {href[:100]}")
            print(f"      [{txt[:120]}]")
            n += 1
            if n >= 8: break

    # Pagination markers
    print("\npagination signals:")
    for kw in ("page=", "next", "pager", "older", "load more"):
        c = len(re.findall(re.escape(kw), html, re.I))
        if c: print(f"  {kw!r}: {c} hits")
    br.close()
