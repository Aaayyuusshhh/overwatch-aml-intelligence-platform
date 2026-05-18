#!/usr/bin/env python3
"""Bank of Baroda — Wilful Defaulters. Parse the CIBIL-format PDF
(/tmp/bob.pdf) with pdfplumber. 17-col canonical schema."""
import csv, pdfplumber
from datetime import datetime, timezone

SID="bob_wilful_defaulters"; AG="Bank of Baroda (BOB)"; LST="Wilful Defaulters List"
PDF="/tmp/bob.pdf"; OUT=f"/home/aayush/risk-pipeline/data/{SID}.csv"
FIELDS=["source_agency","source_list","case_unit","name","father_name","date_of_birth",
 "gender","address","reward_amount","details","has_document","document_url",
 "detail_page_url","interpol_notice_id","link_kind","scraped_at","enrichment_status"]

def run():
    now=datetime.now(timezone.utc).isoformat(); rows=[]
    with pdfplumber.open(PDF) as pdf:
        for pg in pdf.pages:
            for t in pg.extract_tables() or []:
                for r in t:
                    c=[(x or "").strip().replace("\n"," ") for x in r]
                    if len(c)<6: continue
                    blob=" ".join(c).lower()
                    if "borrower" in blob and "member" in blob: continue   # header
                    borrower=c[5] if len(c)>5 else ""
                    if not borrower or borrower.lower() in ("","-","nil"): continue
                    director=c[11] if len(c)>11 else ""
                    guarantor=c[14] if len(c)>14 else ""
                    amt=c[8] if len(c)>8 else ""
                    pan=c[6] if len(c)>6 else ""
                    branch=c[3] if len(c)>3 else ""
                    state=c[4] if len(c)>4 else ""
                    addr=c[7] if len(c)>7 else ""
                    details=(f"Outstanding(Lakh): {amt} | PAN: {pan} | "
                             f"Branch: {branch} | State: {state} | "
                             f"Director/Signatory: {director} | Guarantor: {guarantor} | "
                             f"Reporting: {c[0] if c else ''}")
                    rows.append({"source_agency":AG,"source_list":LST,"case_unit":"",
                        "name":borrower,"father_name":"","date_of_birth":"","gender":"",
                        "address":addr,"reward_amount":"","details":details,
                        "has_document":"No","document_url":"","detail_page_url":
                        "https://bankofbaroda.bank.in/personal-banking/other-services/wilful-defaulter",
                        "interpol_notice_id":"","link_kind":"","scraped_at":now,
                        "enrichment_status":""})
    seen,uniq=set(),[]
    for r in rows:
        k=(r["name"].lower(), r["details"][:80])
        if k in seen: continue
        seen.add(k); uniq.append(r)
    with open(OUT,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore");w.writeheader();w.writerows(uniq)
    print(f"{SID}: {len(uniq)} rows -> {OUT} (empty names: {sum(1 for r in uniq if not r['name'])})")
    for r in uniq[:3]: print(" ",r["name"],"|",r["address"][:40],"|",r["details"][:70])
    return len(uniq)

if __name__=="__main__": run()
