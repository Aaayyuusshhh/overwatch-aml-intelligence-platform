#!/usr/bin/env python3
"""WatchOutInvestors — Consolidated Bank-wise Wilful Defaulters.
Form-POST pagination (form name='watchout', fields comp_name/ddl_quarter/
prevsrch/cnt/currentpage). ~26.8k records @ 22/page. 17-col schema."""
import csv, time, sys, warnings, re, os
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
warnings.filterwarnings("ignore")

SID="watchoutinvestors_wilful_defaulters"
AG="WatchOutInvestors (Consolidated)"; LST="Bank-wise Wilful Defaulters"
URL="https://watchoutinvestors.com/wilful_defaulters.asp"
OUT=f"/home/aayush/risk-pipeline/data/{SID}.csv"
H={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36",
   "Content-Type":"application/x-www-form-urlencoded","Referer":URL}
FIELDS=["source_agency","source_list","case_unit","name","father_name","date_of_birth",
 "gender","address","reward_amount","details","has_document","document_url",
 "detail_page_url","interpol_notice_id","link_kind","scraped_at","enrichment_status"]

def rows_from(html):
    soup=BeautifulSoup(html,"html.parser")
    tabs=soup.find_all("table")
    if not tabs: return []
    big=max(tabs,key=lambda t:len(t.find_all("tr")))
    out=[]
    for tr in big.find_all("tr"):
        c=[x.get_text(" ",strip=True) for x in tr.find_all(["td","th"])]
        if len(c)>=2 and c[0] and c[0].lower()!="defaulter name":
            out.append((c[0], c[1] if len(c)>1 else ""))
    return out

def safe_write(rows, dry_run_max_lost_pct=0.5):
    """Write rows to OUT, but refuse if new data is <50% of existing rows
    (signals partial scrape / network failure, not a genuine site shrink)."""
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                existing = sum(1 for _ in f) - 1  # minus header
        except Exception:
            existing = 0
        if existing > 0 and len(rows) < existing * dry_run_max_lost_pct:
            print(f"  SAFETY: new data has {len(rows)} rows but existing CSV has "
                  f"{existing}. NOT overwriting — looks like a partial/failed scrape.")
            return False
    with open(OUT,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    return True

def run():
    now=datetime.now(timezone.utc).isoformat()
    s=requests.Session()
    r=s.get(URL,headers=H,timeout=30,verify=False)
    m=re.search(r'name="cnt"\s+value="(\d+)"', r.text) or re.search(r"cnt'?\]?\.value\s*=\s*'?(\d+)", r.text)
    total=int(m.group(1)) if m else 27000
    per=22
    pages=(total//per)+2
    print(f"total={total} -> ~{pages} pages")
    rows=[]; seen=set()
    empty_pages = 0   # genuine empty (HTTP 200, parsed, 0 rows) — real end of pagination
    net_errors  = 0   # consecutive ReadTimeout/ConnectionError — DO NOT confuse with empty
    aborted_by_network = False
    for pno in range(1, pages+1):
        data={"comp_name":"","ddl_quarter":"","prevsrch":"","cnt":str(total),
              "currentpage":str(pno)}
        page_failed = False
        try:
            resp=s.post(URL,headers=H,data=data,timeout=30,verify=False)
            recs=rows_from(resp.text)
        except Exception as e:
            print(f"  page {pno}: {type(e).__name__}; retry once")
            time.sleep(2)
            try:
                resp=s.post(URL,headers=H,data=data,timeout=30,verify=False)
                recs=rows_from(resp.text)
            except Exception as e2:
                print(f"  page {pno}: {type(e2).__name__} on retry; NETWORK ERROR")
                recs=[]
                page_failed = True
        if page_failed:
            net_errors += 1
            empty_pages = 0  # reset — this is NOT an empty page, it's an error
            if net_errors >= 3:
                print(f"  3 consecutive network errors at page {pno}; ABORTING without write "
                      f"to preserve existing CSV")
                aborted_by_network = True
                break
            time.sleep(2)
            continue
        # successful HTTP fetch — reset network error counter
        net_errors = 0
        added=0
        for name,amt in recs:
            name=name.strip()
            if not name: continue
            k=(name.lower(),amt.strip())
            if k in seen: continue
            seen.add(k); added+=1
            rows.append({"source_agency":AG,"source_list":LST,"case_unit":"",
                "name":name,"father_name":"","date_of_birth":"","gender":"",
                "address":"","reward_amount":"",
                "details":f"Outstanding Amount (Rs. cr): {amt.strip()} | Consolidated bank-wise wilful defaulter",
                "has_document":"No","document_url":"","detail_page_url":URL,
                "interpol_notice_id":"","link_kind":"","scraped_at":now,
                "enrichment_status":""})
        # genuine empty page (HTTP 200, parsed OK, 0 rows) → real end of pagination
        empty_pages = empty_pages + 1 if added == 0 else 0
        if empty_pages >= 3:
            print(f"  3 genuine empty pages at {pno}; stopping (real end of pagination)")
            break
        if pno%50==0:
            print(f"  page {pno}/{pages}: total {len(rows)}")
            # checkpoint write — but still gated by safe_write
            safe_write(rows)
        time.sleep(0.25)
    if aborted_by_network:
        print(f"{SID}: ABORTED after network errors. {len(rows)} rows fetched; "
              f"existing CSV preserved. Re-run later.")
        return 0
    wrote = safe_write(rows)
    if not wrote:
        print(f"{SID}: refusing to overwrite — existing CSV preserved.")
        return 0
    print(f"{SID}: {len(rows)} rows -> {OUT} (empty names: {sum(1 for r in rows if not r['name'])})")
    for r in rows[:3]: print(" ",r["name"],"|",r["details"][:55])
    return len(rows)

if __name__=="__main__": run()
