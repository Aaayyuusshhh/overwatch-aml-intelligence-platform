#!/usr/bin/env python3
"""Register MCA RD/ROC sources in sources.json."""
import json, os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(PROJECT, "sources.json")

NEW_SOURCES = [
    {
        "id": "mca_disqualified_directors_164",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "Disqualified Directors U/S 164(2)(A)",
        "url": "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/disqualified-directors.html",
        "type": "pdf",
        "scraper": "scrapers/mca_rd_roc.py",
        "expected_min_records": 1000,
        "status": "active",
        "change_detection": False,
        "country": "IN",
        "notes": "159 ROC-wise PDFs via MCA AEM dmslist API. Mix of text-PDFs (parseable) and scanned images (skipped). Parser handles 2 layouts (CIN-first and DIN-first).",
    },
    {
        "id": "mca_companies_struck_off",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "Companies Struck Off (STK-7) U/S 248(5)",
        "url": "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/companies-struck-roc.html",
        "type": "pdf",
        "scraper": "scrapers/mca_rd_roc.py",
        "expected_min_records": 500,
        "status": "active",
        "change_detection": False,
        "country": "IN",
        "notes": "260 ROC-wise PDFs via MCA AEM dmslist API. Some include CIN+English+Hindi columns. Parser extracts CIN-bearing lines.",
    },
    {
        "id": "mca_proclaimed_offenders",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "Proclaimed Offenders U/S 82 Cr.PC",
        "url": "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/proclaimed-offenders.html",
        "type": "pdf",
        "scraper": "scrapers/mca_rd_roc.py",
        "expected_min_records": 50,
        "status": "partial",
        "change_detection": False,
        "country": "IN",
        "notes": "9 PDFs, formats vary widely across ROCs (some with 'S/o' pattern, some tabular). Current parser too strict. Pending heuristic improvements for full coverage.",
    },
    {
        "id": "mca_directors_struck_off_companies",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "Directors Associated with Struck Off Companies U/S 248",
        "url": "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/directors-struck-companies.html",
        "type": "blocked",
        "scraper": None,
        "status": "blocked",
        "change_detection": False,
        "country": "IN",
        "failure_reason": "http_500_from_mca",
        "notes": "MCA page returns 500 error consistently as of 2026-05-25 recon. Retry later.",
    },
    {
        "id": "mca_notice_strike_off",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "Notice of Strike-Off (STK-7) Sec 248(1)",
        "url": "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/notice-strike-off.html",
        "type": "blocked",
        "scraper": None,
        "status": "blocked",
        "change_detection": False,
        "country": "IN",
        "failure_reason": "http_500_from_mca",
        "notes": "MCA page returns 500 error consistently. Retry later.",
    },
    {
        "id": "mca_rd_compounding_orders",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "RD Compounding Orders",
        "url": "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/rd-compounding-orders.html",
        "type": "blocked",
        "scraper": None,
        "status": "blocked",
        "change_detection": False,
        "country": "IN",
        "failure_reason": "http_500_from_mca",
        "notes": "MCA page returns 500. Endpoint uses encrypted filter (/bin/mca/RDAdjudicationOrdersFilter); requires reverse-engineering the AEM encrypt() function.",
    },
    {
        "id": "mca_roc_adjudication_orders",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "ROC Adjudication Orders",
        "url": "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/roc-adjudication-orders.html",
        "type": "html",
        "scraper": None,
        "expected_min_records": 0,
        "status": "no_data",
        "change_detection": False,
        "country": "IN",
        "failure_reason": "no_data_published",
        "notes": "Page loads (folder 479) but API returns totalResults=0. List currently empty.",
    },
]


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
        print(f"  add: {new['id']:42s}  status={new['status']}")
        added += 1
    with open(SRC_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nAdded {added}, skipped {skipped}. Total sources: {len(data['sources'])}")


if __name__ == "__main__":
    main()
