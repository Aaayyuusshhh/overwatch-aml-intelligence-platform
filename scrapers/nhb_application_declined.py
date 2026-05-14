"""
NHB Companies whose Application for Certificate of Registration have
been Declined / Rejected / Withdrawn (#91).

Source: https://nhb.org.in/companies-whose-application

The page has a Sl.No / Name / Address table. The generic engine took
'Sl.' as the name column because it appears in column 0 of the header.
This custom scraper hard-codes column 1 = Name of the Company.
"""

import csv
import os
import re
from datetime import datetime

from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://nhb.org.in/companies-whose-application"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                           "nhb_companies_whose_application_for_91.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _clean(s):
    if s is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", s).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def scrape():
    r = Fetcher.get(LIST_URL, timeout=45, retries=1, retry_delay=0, verify=False)
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", body, re.I)
    best = []
    for t in tables:
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
        if len(trs) > len(best):
            best = trs
    if not best:
        raise RuntimeError("NHB #91: no table found on page")

    parsed = [[_clean(c) for c in re.findall(
        r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)] for tr in best]

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for cells in parsed:
        if len(cells) < 2:
            continue
        sno  = cells[0]
        name = cells[1]
        addr = cells[2] if len(cells) > 2 else ""
        # Skip header
        if name.lower().startswith("name") or "name of" in name.lower():
            continue
        if not name or len(name) < 4:
            continue
        out.append({
            "source_agency": "NHB",
            "source_list": "Companies whose Application for Certificate of Registration Declined/Rejected/Withdrawn",
            "case_unit": sno,
            "name": name,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": addr,
            "reward_amount": "",
            "details": f"S.No: {sno}",
            "has_document": "No",
            "document_url": "",
            "detail_page_url": LIST_URL,
            "interpol_notice_id": "",
            "link_kind": "nhb_application_declined",
            "scraped_at": scraped_at,
            "enrichment_status": "none",
        })
    return out


def save_to_csv(rows, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {out_path}")


def run():
    print("=" * 60)
    print("NHB Application Declined/Rejected scraper (#91)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("NHB #91: zero rows extracted")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
