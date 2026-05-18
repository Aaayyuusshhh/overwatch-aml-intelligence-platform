#!/usr/bin/env python3
"""Punjab National Bank — Wilful Defaulters (Suit Filed). Downloads the
latest 'List of Suit Filed cases' Excel via Playwright (ASP.NET postback,
served as .xlsx), parses with pandas. 17-col canonical schema."""
import csv, time, os
from datetime import datetime, timezone
import pandas as pd
from playwright.sync_api import sync_playwright

SID="pnb_wilful_defaulters"; AG="Punjab National Bank (PNB)"
LST="Wilful Defaulters - Suit Filed Cases"
PAGE="https://pnb.bank.in/wilful-defaulters.html"
XLS="/tmp/pnb_wd.xlsx"; OUT=f"/home/aayush/risk-pipeline/data/{SID}.csv"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
FIELDS=["source_agency","source_list","case_unit","name","father_name","date_of_birth",
 "gender","address","reward_amount","details","has_document","document_url",
 "detail_page_url","interpol_notice_id","link_kind","scraped_at","enrichment_status"]

def download():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        ctx=b.new_context(user_agent=UA,ignore_https_errors=True,accept_downloads=True)
        pg=ctx.new_page()
        pg.goto(PAGE,wait_until="domcontentloaded",timeout=45000)
        try: pg.wait_for_load_state("networkidle",timeout=15000)
        except: pass
        time.sleep(4)
        loc=pg.locator('a:has-text("List of Suit Filed cases Wilful Defaulters")').first
        with pg.expect_download(timeout=45000) as di:
            loc.click()
        di.value.save_as(XLS)
        b.close()

def run():
    download()
    now=datetime.now(timezone.utc).isoformat()
    df=pd.read_excel(XLS, sheet_name=0, header=0, dtype=str).fillna("")
    cols=list(df.columns)
    def col(i): return cols[i] if i < len(cols) else None
    rows=[]
    for _,r in df.iterrows():
        borrower=str(r.get(col(5),"")).strip()
        if not borrower or borrower.lower() in ("nan","borrower name",""): continue
        pan=str(r.get(col(6),"")).strip()
        addr=str(r.get(col(7),"")).strip()
        amt=str(r.get(col(8),"")).strip()
        director=str(r.get(col(11),"")).strip()
        guar=str(r.get(col(14),"")).strip()
        branch=str(r.get(col(3),"")).strip()
        state=str(r.get(col(4),"")).strip()
        details=(f"Outstanding(Lakh): {amt} | PAN: {pan} | Branch: {branch} | "
                 f"State: {state} | Suit Status: {str(r.get(col(9),'')).strip()} | "
                 f"Director/Promoter: {director} | Guarantor: {guar}")
        rows.append({"source_agency":AG,"source_list":LST,"case_unit":"",
            "name":borrower,"father_name":"","date_of_birth":"","gender":"",
            "address":addr,"reward_amount":"","details":details,
            "has_document":"No","document_url":"","detail_page_url":PAGE,
            "interpol_notice_id":"","link_kind":"","scraped_at":now,
            "enrichment_status":""})
    seen,uniq=set(),[]
    for r in rows:
        k=(r["name"].lower(),r["details"][:90])
        if k in seen: continue
        seen.add(k); uniq.append(r)
    with open(OUT,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore");w.writeheader();w.writerows(uniq)
    print(f"{SID}: {len(uniq)} rows -> {OUT} (empty names: {sum(1 for r in uniq if not r['name'])})")
    for r in uniq[:3]: print(" ",r["name"],"|",r["address"][:40],"|",r["details"][:70])
    return len(uniq)

if __name__=="__main__": run()
