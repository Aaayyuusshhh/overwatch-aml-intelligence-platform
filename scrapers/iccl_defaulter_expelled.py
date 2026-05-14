"""
ICCL Defaulter & Expelled Members (#166).

Source: https://www.icclindia.com/downloads/Default_&_Expelled_Members.pdf
Discovered via /Static/membership/regulatory_actions.aspx on
icclindia.com (the listed /defaulter-expelled-members route is an
empty stub; the real list lives in the PDF).

PDF columns:
  Sr. No. | SEBI Registration No | Clg. No. | Member Name |
  Date of Action | Defaulter/Expelled | Remarks

A handful of rows have two action events for the same member (e.g.
expelled first, declared defaulter later). pdfplumber returns those
as a primary row plus a "continuation" row with empty Sr.No. /
Member columns; we merge the continuation into the primary row's
details so the member still appears as one watchlist entry.
"""

import csv
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import pdfplumber
import requests
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_URL = "https://www.icclindia.com/downloads/Default_&_Expelled_Members.pdf"
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "raw",
                        "iccl_default_expelled_members.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "iccl_defaulter_expelled_166.csv")
DETAIL_PAGE = "https://www.icclindia.com/Static/membership/regulatory_actions.aspx"

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/120.0.0.0 Safari/537.36",
      "Referer": "https://www.icclindia.com/"}


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _ensure_pdf():
    os.makedirs(os.path.dirname(PDF_PATH), exist_ok=True)
    if os.path.exists(PDF_PATH) and os.path.getsize(PDF_PATH) > 10_000:
        return
    r = requests.get(PDF_URL, headers=UA, timeout=60, verify=False)
    if r.status_code != 200 or not r.content[:8].lstrip().startswith(b"%PDF"):
        raise RuntimeError(f"ICCL PDF download failed: status={r.status_code}")
    with open(PDF_PATH, "wb") as f:
        f.write(r.content)


def scrape():
    _ensure_pdf()
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    cur = None
    with pdfplumber.open(PDF_PATH) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for raw in table:
                    cells = [_clean(c) for c in raw]
                    if len(cells) < 7:
                        cells += [""] * (7 - len(cells))
                    sn, sebi_reg, clg, name, action_date, action, remarks = cells[:7]
                    # Skip header
                    if sn.lower() in {"sr. no.", "sr.no.", "s.no."}:
                        continue
                    if not sn and not name:
                        # Continuation row carrying an extra action date for
                        # the previous member.
                        if cur is not None and action_date:
                            extra = (f"Additional Action: {action} on {action_date}"
                                      + (f" — {remarks}" if remarks else ""))
                            cur["details"] = (cur["details"] + " | " + extra
                                              if cur["details"] else extra)
                        continue
                    if not name:
                        continue
                    # finalise the previous one
                    if cur is not None:
                        rows.append(cur)
                    detail_parts = []
                    if action_date:
                        detail_parts.append(f"Date of Action: {action_date}")
                    if action:
                        detail_parts.append(f"Status: {action}")
                    if sebi_reg:
                        detail_parts.append(f"SEBI Reg No: {sebi_reg}")
                    if clg:
                        detail_parts.append(f"Clg No: {clg}")
                    if remarks:
                        detail_parts.append(f"Remarks: {remarks}")
                    cur = {
                        "source_agency": "Indian Clearing Corporation Limited (ICCL)",
                        "source_list":   "Defaulter & Expelled Members",
                        "case_unit":     sebi_reg or clg,
                        "name":          name,
                        "father_name":   "",
                        "date_of_birth": "",
                        "gender":        "",
                        "address":       "",
                        "reward_amount": "",
                        "details":       " | ".join(detail_parts),
                        "has_document":  "Yes",
                        "document_url":  PDF_URL,
                        "detail_page_url": DETAIL_PAGE,
                        "interpol_notice_id": "",
                        "link_kind":     "manual_discovery",
                        "scraped_at":    scraped_at,
                        "enrichment_status": "",
                    }
    if cur is not None:
        rows.append(cur)
    return rows


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
    print("ICCL Defaulter & Expelled Members (#166)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("ICCL: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
