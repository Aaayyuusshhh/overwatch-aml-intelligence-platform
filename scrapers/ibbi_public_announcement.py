"""
IBBI Public Announcement (#244).

Source: IBBI exports its public-announcement search as an .xls file —
but the file is actually **TAB-separated text** with an .xls extension
(no OLE / no zip, header "Announcement Type<TAB>Date of Announcement..."
). Pandas/xlrd/openpyxl all reject it, so we parse with csv.reader on
'\\t' delimiter.

Pre-staged at: data/PUBLIC_ANNOUNCEMENT12-05-26_12_29.xls
(14,323 records as of this scrape)

Columns:
  Announcement Type | Date of Announcement | Last date of Submission |
  Name of Corporate Debtor | CIN No. | Name of Applicant |
  Name of Insolvency Professional | Address of Insolvency Professional |
  Remarks
"""

import csv
import html
import os
import re
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                          "PUBLIC_ANNOUNCEMENT12-05-26_12_29.xls")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                          "ibbi_public_announcement_244.csv")
DETAIL_PAGE = "https://www.ibbi.gov.in/en/public-announcement"

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
    s = html.unescape(str(s))            # decode &amp;, &#39;, etc.
    s = s.replace("\xa0", " ").replace("–", "-")
    s = s.replace(",,", ", ")            # IBBI's address fields use ",,," as spaces
    s = re.sub(r"\s+", " ", s).strip(" .,;-")
    return s


def scrape():
    if not os.path.exists(INPUT_FILE):
        raise RuntimeError(f"IBBI: input file missing at {INPUT_FILE}")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    with open(INPUT_FILE, encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        headers = [h.strip() for h in next(reader)]
        # Map header position by label to be robust to column-order
        # changes.
        idx = {h: i for i, h in enumerate(headers)}
        def col(row, key, default=""):
            i = idx.get(key)
            return _clean(row[i]) if i is not None and i < len(row) else default

        for row in reader:
            name = col(row, "Name of Corporate Debtor")
            if not name:
                continue
            atype  = col(row, "Announcement Type")
            adate  = col(row, "Date of Announcement")
            ldate  = col(row, "Last date of Submission")
            cin    = col(row, "CIN No.")
            apl    = col(row, "Name of Applicant")
            ip_nm  = col(row, "Name of Insolvency Professional")
            ip_addr= col(row, "Address of Insolvency Professional")
            remarks= col(row, "Remarks")
            detail_parts = []
            if atype:   detail_parts.append(f"Type: {atype}")
            if cin:     detail_parts.append(f"CIN: {cin}")
            if apl:     detail_parts.append(f"Applicant: {apl}")
            if ip_nm:   detail_parts.append(f"IP: {ip_nm}")
            if adate:   detail_parts.append(f"Date: {adate}")
            if ldate:   detail_parts.append(f"Deadline: {ldate}")
            if ip_addr: detail_parts.append(f"IP Address: {ip_addr}")
            if remarks: detail_parts.append(f"Remarks: {remarks}")
            out.append({
                "source_agency": "Insolvency and Bankruptcy Board of India (IBBI)",
                "source_list":   "Public Announcement",
                "case_unit":     cin,
                "name":          name,
                "father_name":   "",
                "date_of_birth": "",
                "gender":        "",
                "address":       ip_addr,
                "reward_amount": "",
                "details":       " | ".join(detail_parts),
                "has_document":  "No",
                "document_url":  "",
                "detail_page_url": DETAIL_PAGE,
                "interpol_notice_id": "",
                "link_kind":     "manual_discovery",
                "scraped_at":    scraped_at,
                "enrichment_status": "",
            })
    return out


def save_to_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {path}")


def run():
    print("=" * 60)
    print("IBBI Public Announcement (#244)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("IBBI: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
