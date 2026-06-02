#!/usr/bin/env python3
"""Bank of Baroda — Wilful Defaulters. Parse the CIBIL-format PDF
with pdfplumber. 17-col canonical schema.

BoB publishes a fresh monthly wilful-defaulter PDF; this scraper
discovers the latest link from the public landing page each run, so
it auto-tracks month-to-month renames. Falls back to a pre-staged
/tmp/bob.pdf if discovery fails (preserves the old manual workflow)."""
import csv, os, re, pdfplumber, requests
from datetime import datetime, timezone

SID="bob_wilful_defaulters"; AG="Bank of Baroda (BOB)"; LST="Wilful Defaulters List"
PAGE="https://bankofbaroda.bank.in/personal-banking/other-services/wilful-defaulter"
PDF="/tmp/bob.pdf"; OUT=f"/home/aayush/risk-pipeline/data/{SID}.csv"
UA={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":"en-US,en;q=0.9"}
FIELDS=["source_agency","source_list","case_unit","name","father_name","date_of_birth",
 "gender","address","reward_amount","details","has_document","document_url",
 "detail_page_url","interpol_notice_id","link_kind","scraped_at","enrichment_status"]


def _discover_pdf_url():
    """Return the most recent Wilful-Defaulter-Data PDF URL on the BoB page,
    or None if the page can't be fetched or no matching link is present."""
    try:
        r = requests.get(PAGE, headers=UA, timeout=25, verify=False)
        if r.status_code != 200:
            return None
    except Exception:
        return None
    # Prefer "Wilful-Defaulter-Data-<Month>-<Year>.pdf"; fall back to any PDF
    # under the same /pdfs2/ media path containing 'WD' or 'Wilful'.
    cands = re.findall(
        r'href="(/-/media/[^"]*Wilful[^"]*\.pdf)"', r.text, re.I)
    if not cands:
        cands = re.findall(
            r'href="(/-/media/[^"]*(?:WD|Wilful-Defaulter)[^"]*\.pdf)"',
            r.text, re.I)
    if not cands:
        return None
    # Pick the latest by lexicographic order (paths embed YYYY/YY-MM/);
    # that gives newest first.
    href = sorted(cands)[-1]
    return "https://bankofbaroda.bank.in" + href


def _ensure_pdf():
    """Download the latest wilful-defaulter PDF to PDF unless a stale copy
    sits there already. Returns the local path."""
    url = _discover_pdf_url()
    if url is None:
        if os.path.exists(PDF) and os.path.getsize(PDF) > 50_000:
            print(f"  using cached {PDF} (discovery failed)")
            return PDF
        raise RuntimeError("BoB: PDF discovery failed and no /tmp/bob.pdf "
                           "to fall back on")
    print(f"  downloading {url}")
    r = requests.get(url, headers=UA, timeout=120, verify=False, stream=True)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"BoB: PDF download HTTP {r.status_code}")
    with open(PDF, "wb") as f:
        f.write(r.content)
    return PDF


def run():
    now=datetime.now(timezone.utc).isoformat(); rows=[]
    _ensure_pdf()
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
