"""
Bank of Maharashtra — Wilful Defaulters (#132).

Source: https://bankofmaharashtra.bank.in/wilful-defaulters
PDF layout matches the standard CIBIL 17-column export (same shape
that BOB and IOB use):

  Reporting Cycle | Member ID | Member Name | Branch | State |
  Borrower Name | Borrower PAN | Borrower Address | Outstanding (Lakhs) |
  Suit Status | Other Member | Director Name | Director DIN |
  Director PAN | Guarantor Name | Guarantor CIN | Guarantor PAN

Rows repeat per related party (director/guarantor) for the same
borrower. We emit one record per row so downstream entity-resolution
can dedupe.
"""

import csv
import os
import re
from datetime import datetime

import pdfplumber
import requests
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://bankofmaharashtra.bank.in/wilful-defaulters"
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "bom_wilful_defaulters.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "bom_wilful_defaulters_132.csv")

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
      "Referer": "https://bankofmaharashtra.bank.in/"}


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ").replace("\n", " ")).strip()


def _ensure_pdf():
    """Use the pre-staged local PDF if present; otherwise try to
    discover and download the latest one from the listing page."""
    if os.path.exists(PDF_PATH) and os.path.getsize(PDF_PATH) > 200_000:
        return PDF_PATH
    r = requests.get(LIST_URL, headers=UA, timeout=45, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"BOM: list page status {r.status_code}")
    hrefs = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', r.text, re.I)
    wfd = [h for h in hrefs if "wilful" in h.lower() or "wfd" in h.lower()
                                or "defaulter" in h.lower()]
    if not wfd:
        raise RuntimeError("BOM: no Wilful Defaulter PDF found on list page")
    from urllib.parse import urljoin
    url = urljoin(LIST_URL, wfd[0])
    print(f"  downloading {url}")
    rr = requests.get(url, headers=UA, timeout=120, verify=False)
    if rr.status_code != 200 or not rr.content[:8].lstrip().startswith(b"%PDF"):
        raise RuntimeError(f"BOM: PDF download failed (status={rr.status_code})")
    os.makedirs(os.path.dirname(PDF_PATH), exist_ok=True)
    with open(PDF_PATH, "wb") as f:
        f.write(rr.content)
    return PDF_PATH


def scrape():
    pdf = _ensure_pdf()
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    doc_url = LIST_URL
    with pdfplumber.open(pdf) as pdfobj:
        n_pages = len(pdfobj.pages)
        for page in pdfobj.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [_clean(c) for c in (row + [""] * 17)[:17]]
                    cycle, member_id, member_name, branch, state, \
                        b_name, b_pan, b_addr, amt, suit_status, \
                        other_mem, d_name, din, d_pan, \
                        g_name, g_cin, g_pan = cells
                    if cycle.lower().startswith("reporting"):
                        continue
                    if not (b_name or d_name or g_name):
                        continue
                    name = b_name or d_name or g_name
                    detail_parts = []
                    if member_name:  detail_parts.append(f"Member: {member_name}")
                    if branch:       detail_parts.append(f"Branch: {branch}")
                    if state:        detail_parts.append(f"State: {state}")
                    if d_name:       detail_parts.append(
                        f"Director: {d_name}" + (f" (DIN {din})" if din else ""))
                    if d_pan:        detail_parts.append(f"Director PAN: {d_pan}")
                    if g_name:       detail_parts.append(f"Guarantor: {g_name}")
                    if g_cin:        detail_parts.append(f"Guarantor CIN: {g_cin}")
                    if g_pan:        detail_parts.append(f"Guarantor PAN: {g_pan}")
                    if suit_status:  detail_parts.append(f"Suit Status: {suit_status}")
                    if cycle:        detail_parts.append(f"Reporting Cycle: {cycle}")
                    key = (name.lower(), b_pan, d_name.lower(), g_name.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "source_agency": "Bank of Maharashtra (BOM)",
                        "source_list":   "Wilful Defaulters",
                        "case_unit":     b_pan or din or g_pan,
                        "name":          name,
                        "father_name":   "",
                        "date_of_birth": "",
                        "gender":        "",
                        "address":       b_addr,
                        "reward_amount": amt,
                        "details":       " | ".join(detail_parts),
                        "has_document":  "Yes",
                        "document_url":  doc_url,
                        "detail_page_url": LIST_URL,
                        "interpol_notice_id": "",
                        "link_kind":     "manual_discovery",
                        "scraped_at":    scraped_at,
                        "enrichment_status": "",
                    })
    print(f"  parsed {len(out)} records from {n_pages} pages")
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
    print("Bank of Maharashtra Wilful Defaulters (#132)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("BOM: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
