#!/usr/bin/env python3
"""WatchOutInvestors — Consolidated Bank-wise Wilful Defaulters.
Form-POST pagination (form name='watchout', fields comp_name/ddl_quarter/
prevsrch/cnt/currentpage). ~26.8k records @ 22/page. 17-col schema."""
import csv, time, sys, warnings, re
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

def run():
    now=datetime.now(timezone.utc).isoformat()
    s=requests.Session()
    r=s.get(URL,headers=H,timeout=30,verify=False)
    m=re.search(r'name="cnt"\s+value="(\d+)"', r.text) or re.search(r"cnt'?\]?\.value\s*=\s*'?(\d+)", r.text)
    total=int(m.group(1)) if m else 27000
    per=22
    pages=(total//per)+2
    print(f"total={total} -> ~{pages} pages")
    rows=[]; seen=set(); empty=0
    for pno in range(1, pages+1):
        data={"comp_name":"","ddl_quarter":"","prevsrch":"","cnt":str(total),
              "currentpage":str(pno)}
        try:
            resp=s.post(URL,headers=H,data=data,timeout=30,verify=False)
            recs=rows_from(resp.text)
        except Exception as e:
            print(f"  page {pno}: {type(e).__name__}; retry once")
            time.sleep(2)
            try:
                resp=s.post(URL,headers=H,data=data,timeout=30,verify=False)
                recs=rows_from(resp.text)
            except Exception:
                recs=[]
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
        empty = empty+1 if added==0 else 0
        if empty>=3:
            print(f"  3 empty pages at {pno}; stopping"); break
        if pno%50==0:
            print(f"  page {pno}/{pages}: total {len(rows)}")
            with open(OUT,"w",newline="",encoding="utf-8") as f:  # checkpoint
                w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore")
                w.writeheader(); w.writerows(rows)
        time.sleep(0.25)
    with open(OUT,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"{SID}: {len(rows)} rows -> {OUT} (empty names: {sum(1 for r in rows if not r['name'])})")
    for r in rows[:3]: print(" ",r["name"],"|",r["details"][:55])
    return len(rows)

if __name__=="__main__": run()
