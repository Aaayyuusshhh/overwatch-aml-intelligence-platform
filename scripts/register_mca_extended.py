#!/usr/bin/env python3
"""Register all extended MCA sources in sources.json.

Part A (5): defaulter_companies, defaulter_directors, dormant_companies,
            llps_strike_off, public_notices_stk6
Part B   : roc_adjudication_orders (already registered as no_data — confirmed)
Part C   : mca_corporate_fraud_chit_fund (TaxGuru)
Part D   : mca_vanishing_companies (WatchOut mirror)
Part E   : 19 EES legacy PDFs (all return 2654-byte stub — register as url_not_found)
Part F   : 3 retry sources (still HTTP 500 — confirmed blocked)
"""
import json, os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(PROJECT, "sources.json")

EES_BASE = "http://www.mca.gov.in/MCA21/dca/EES_Companies_List/"
EES_PDFS = [
    "CompanyListA_F.pdf", "CompanyListG_L.pdf", "CompanyListM_R.pdf", "CompanyListS_Z.pdf",
    "DIRLIST1_00000000_00120000.pdf", "DIRLIST2_00120000_00300000.pdf",
    "DIRLIST3_00300000_00500000.pdf", "DIRLIST4_00500000_00750000.pdf",
    "DIRLIST5_00750000_01050000.pdf", "DIRLIST6_01050000_01300000.pdf",
    "DIRLIST7_01300000_01600000.pdf", "DIRLIST8_01600000_01750000.pdf",
    "DIRLIST9_01750000_01900000.pdf", "DIRLIST10_01900000_02050000.pdf",
    "DIRLIST11_02050000_02200000.pdf", "DIRLIST12_02200000_02400000.pdf",
    "DIRLIST13_02400000_02700000.pdf", "DIRLIST14_02700000_00400000.pdf",
    "SEC_SINGLE_LIST.pdf",
]

NEW_SOURCES = [
    # Part A
    {"id":"mca_defaulter_companies",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"Defaulter Companies (Filing Default)",
     "url":"https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/defaulter-companies.html",
     "type":"pdf","scraper":"scrapers/mca_rd_roc.py",
     "expected_min_records":100,"status":"active","change_detection":False,
     "country":"IN",
     "notes":"15 PDFs via AEM folder 382. Most are scanned images; 1 of 5 sampled was text-parseable yielding ~900 rows."},
    {"id":"mca_defaulter_directors",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"Defaulter Directors (Filing Default)",
     "url":"https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/defaulter-directors.html",
     "type":"pdf","scraper":"scrapers/mca_rd_roc.py",
     "expected_min_records":0,"status":"partial","change_detection":False,
     "country":"IN",
     "notes":"15 PDFs via AEM folder 383, mostly scanned images. Parser yields few rows. Future: OCR pass."},
    {"id":"mca_dormant_companies",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"Dormant Companies (3yr Filing Default)",
     "url":"https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/dormant-companies.html",
     "type":"pdf","scraper":"scrapers/mca_rd_roc.py",
     "expected_min_records":500,"status":"active","change_detection":False,
     "country":"IN",
     "notes":"14 PDFs via AEM folder 384. Sampled 5; yielded 2164 rows of dormant company CINs."},
    {"id":"mca_llps_strike_off",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"LLPs Under Process of Strike Off",
     "url":"https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/llps-under-strike-off.html",
     "type":"pdf","scraper":"scrapers/mca_rd_roc.py",
     "expected_min_records":0,"status":"partial","change_detection":False,
     "country":"IN",
     "notes":"7 PDFs via AEM folder 386, all scanned images on first inspection. Future: OCR."},
    {"id":"mca_public_notices_stk6",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"Public Notices (STK-6) U/S 248(2)",
     "url":"https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/public-notices-stk6.html",
     "type":"pdf","scraper":"scrapers/mca_rd_roc.py",
     "expected_min_records":200,"status":"active","change_detection":False,
     "country":"IN",
     "notes":"2458 PDFs via AEM folder 1443. Each notice lists 5-15 companies with CIN. Scrape top-N by date; recent ones (May 2026) are clean text."},
    # Part C
    {"id":"mca_corporate_fraud_chit_fund",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"Companies Involved in Corporate Frauds / Chit Fund Scams",
     "url":"https://taxguru.in/corporate-law/list-companies-involved-corporate-fraudschit-fund-scams.html",
     "type":"html","scraper":"scrapers/mca_taxguru_frauds.py",
     "expected_min_records":100,"status":"active","change_detection":False,
     "country":"IN",
     "notes":"145 MCA-investigated frauds (illegal deposits / chit funds). Source: TaxGuru article reproducing the official annexure."},
    # Part D
    {"id":"mca_vanishing_companies",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"Vanishing Companies (MCA via WatchOut)",
     "url":"https://www.watchoutinvestors.com/dcavanish.asp?id=1181227",
     "type":"html","scraper":"scrapers/mca_vanishing_companies.py",
     "expected_min_records":50,"status":"active","change_detection":False,
     "country":"IN",
     "notes":"MCA Vanishing Companies list mirrored at WatchOut. ~20 companies + their directors/officers. MCA does not host its own copy any more."},
    # Part F retries — still 500 as of 2026-05-25, register as confirmed blocked
    {"id":"mca_directors_struck_off_companies_v2",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"Directors of Struck-Off Companies (U/S 248)",
     "url":"https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/directors-struck-companies.html",
     "type":"blocked","scraper":None,"status":"blocked",
     "change_detection":False,"country":"IN",
     "failure_reason":"http_500_persistent",
     "notes":"MCA page returns 500. Retried 3 times on 2026-05-25 — still 500. Server-side issue."},
    {"id":"mca_notice_strike_off_v2",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"Notice of Strike-Off (STK-7) Sec 248(1) [retry]",
     "url":"https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/notice-strike-off.html",
     "type":"blocked","scraper":None,"status":"blocked",
     "change_detection":False,"country":"IN",
     "failure_reason":"http_500_persistent",
     "notes":"Retried 3 times 2026-05-25; still 500."},
    {"id":"mca_rd_compounding_orders_v2",
     "agency":"Ministry of Corporate Affairs (MCA)",
     "list_name":"RD Compounding Orders [retry]",
     "url":"https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/rd-compounding-orders.html",
     "type":"blocked","scraper":None,"status":"blocked",
     "change_detection":False,"country":"IN",
     "failure_reason":"http_500_persistent",
     "notes":"Retried 3 times 2026-05-25; still 500."},
]

# Part E — 19 EES legacy PDF URLs, all dead (return 2654-byte stub)
for fname in EES_PDFS:
    sid = f"mca_ees_{fname[:-4].lower()}"
    NEW_SOURCES.append({
        "id": sid,
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": f"EES Legacy List ({fname})",
        "url": EES_BASE + fname,
        "type": "url_not_found",
        "scraper": None,
        "status": "url_not_found",
        "change_detection": False,
        "country": "IN",
        "failure_reason": "url_returns_stub_2654_bytes",
        "notes": ("Legacy MCA21/dca/EES_Companies_List/ PDFs from 2011 era. URL still returns "
                  "200 OK but the response is a 2654-byte error stub, not a real PDF. Resource "
                  "removed; original DEFAULTERS.doc lists 19 such URLs all confirmed dead."),
    })


def main():
    with open(SRC_PATH) as f:
        data = json.load(f)
    existing_ids = {s.get("id") for s in data["sources"]}
    added, skipped = 0, 0
    for new in NEW_SOURCES:
        if new["id"] in existing_ids:
            print(f"  skip (exists): {new['id']}")
            skipped += 1
            continue
        data["sources"].append(new)
        print(f"  add: {new['id']:50s}  status={new['status']}")
        added += 1
    with open(SRC_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nAdded {added}, skipped {skipped}. Total sources: {len(data['sources'])}")


if __name__ == "__main__":
    main()
