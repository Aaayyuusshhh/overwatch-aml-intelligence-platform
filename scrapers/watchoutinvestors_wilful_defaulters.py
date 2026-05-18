#!/usr/bin/env python3
"""WatchOutInvestors — Consolidated Bank-wise Wilful Defaulters.
Paginated via JS SearchData(n). 17-col canonical schema."""
import csv, time
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

SID="watchoutinvestors_wilful_defaulters"
AG="WatchOutInvestors (Consolidated)"; LST="Bank-wise Wilful Defaulters"
URL="https://watchoutinvestors.com/wilful_defaulters.asp"
OUT=f"/home/aayush/risk-pipeline/data/{SID}.csv"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
FIELDS=["source_agency","source_list","case_unit","name","father_name","date_of_birth",
 "gender","address","reward_amount","details","has_document","document_url",
 "detail_page_url","interpol_notice_id","link_kind","scraped_at","enrichment_status"]

def biggest_rows(html):
    soup=BeautifulSoup(html,"html.parser")
    tabs=soup.find_all("table")
    if not tabs: return []
    big=max(tabs,key=lambda t:len(t.find_all("tr")))
    out=[]
    for tr in big.find_all("tr"):
        cells=[c.get_text(" ",strip=True) for c in tr.find_all(["td","th"])]
        if len(cells)>=2 and cells[0] and cells[0].lower()!="defaulter name":
            out.append(cells)
    return out

def run():
    now=datetime.now(timezone.utc).isoformat(); rows=[]; seen=set()
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        ctx=b.new_context(user_agent=UA,viewport={"width":1920,"height":1080},ignore_https_errors=True)
        pg=ctx.new_page()
        pg.goto(URL,wait_until="domcontentloaded",timeout=45000); time.sleep(3)
        page_no=1; empty=0
        while page_no<=400:
            # robust content read (page may be mid form-POST navigation)
            html=None
            for _ in range(6):
                try:
                    html=pg.content(); break
                except Exception:
                    time.sleep(1.5)
            if html is None:
                print(f"  content unavailable at page {page_no}; stop"); break
            recs=biggest_rows(html)
            added=0
            for c in recs:
                name=c[0].strip()
                amt=c[1].strip() if len(c)>1 else ""
                if not name: continue
                k=(name.lower(),amt)
                if k in seen: continue
                seen.add(k); added+=1
                rows.append({"source_agency":AG,"source_list":LST,"case_unit":"",
                    "name":name,"father_name":"","date_of_birth":"","gender":"",
                    "address":"","reward_amount":"",
                    "details":f"Outstanding Amount (Rs. cr): {amt} | Consolidated bank-wise wilful defaulter",
                    "has_document":"No","document_url":"","detail_page_url":URL,
                    "interpol_notice_id":"","link_kind":"","scraped_at":now,
                    "enrichment_status":""})
            if added==0:
                empty+=1
            else:
                empty=0
            if empty>=2:
                break
            if page_no%20==0:
                print(f"  page {page_no}: total {len(rows)}")
            page_no+=1
            try:
                # SearchData(n) submits a form -> full navigation
                try:
                    with pg.expect_navigation(timeout=20000):
                        pg.evaluate(f"SearchData({page_no})")
                except Exception:
                    # some impls re-render w/o nav event; just wait
                    time.sleep(2)
                try:
                    pg.wait_for_load_state("networkidle",timeout=15000)
                except Exception:
                    pass
                time.sleep(1.0)
            except Exception as e:
                print(f"  pagination stop at {page_no}: {type(e).__name__}")
                break
        b.close()
    with open(OUT,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore");w.writeheader();w.writerows(rows)
    print(f"{SID}: {len(rows)} rows -> {OUT} (empty names: {sum(1 for r in rows if not r['name'])})")
    for r in rows[:3]: print(" ",r["name"],"|",r["details"][:60])
    return len(rows)

if __name__=="__main__": run()
