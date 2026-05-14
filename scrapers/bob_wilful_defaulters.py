"""
Bank of Baroda — Wilful Defaulters (#130).

Source: https://bankofbaroda.bank.in/personal-banking/other-services/wilful-defaulter
That page links 57+ monthly PDF snapshots (WFD-<MMM>-<YYYY>-...pdf).
We grab the most recent one. PDF layout is the standard CIBIL/RBI
17-column wilful-defaulter export (same shape IOB uses):

  Reporting Cycle | Member ID | Member Name | Branch | State |
  Borrower Name | Borrower PAN | Borrower Address | Outstanding (Lakhs) |
  Suit Status | Other Member | Director Name | Director DIN |
  Director PAN | Guarantor Name | Guarantor CIN | Guarantor PAN

Rows repeat per director/guarantor for the same borrower. We emit one
record per row (preserving DIN/PAN of every related party) so
downstream entity-resolution can dedupe.
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
LIST_URL = ("https://bankofbaroda.bank.in/personal-banking/"
            "other-services/wilful-defaulter")
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "raw", "bob_wfd_latest.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "bob_wilful_defaulters_130.csv")

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
      "Referer": "https://bankofbaroda.bank.in/"}

MONTH_RE = re.compile(
    r"WFD[-_\s]*(JAN|FEB|MAR|APR|MAY|JUNE?|JULY?|AUG|SEPT?|OCT|NOV|DEC)"
    r"[-_\s]*(\d{4})", re.I,
)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _discover_latest_pdf():
    r = requests.get(LIST_URL, headers=UA, timeout=45, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"BOB: list page status {r.status_code}")
    hrefs = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']',
                       r.text, re.I)
    wfd = [h for h in hrefs if "wfd" in h.lower() or "wilful" in h.lower()]
    def _key(h):
        m = MONTH_RE.search(h)
        if not m:
            return (0, 0)
        months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"june":6,
                  "jul":7,"july":7,"aug":8,"sep":9,"sept":9,"oct":10,
                  "nov":11,"dec":12}
        return (int(m.group(2)), months.get(m.group(1).lower(), 0))
    if not wfd:
        raise RuntimeError("BOB: no WFD PDF found on list page")
    latest = sorted(wfd, key=_key, reverse=True)[0]
    return urljoin(LIST_URL, latest)


def _ensure_pdf(force=False):
    """Download (or reuse cached) latest WFD PDF."""
    if (not force) and os.path.exists(PDF_PATH) and os.path.getsize(PDF_PATH) > 200_000:
        return PDF_PATH
    url = _discover_latest_pdf()
    print(f"  downloading {url}")
    r = requests.get(url, headers=UA, timeout=120, verify=False)
    if r.status_code != 200 or not r.content[:8].lstrip().startswith(b"%PDF"):
        raise RuntimeError(f"BOB: PDF download failed (status={r.status_code})")
    os.makedirs(os.path.dirname(PDF_PATH), exist_ok=True)
    with open(PDF_PATH, "wb") as f:
        f.write(r.content)
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
                    # Skip header rows (start with "Reporting")
                    if cycle.lower().startswith("reporting"):
                        continue
                    if not (b_name or d_name or g_name):
                        continue
                    name = b_name or d_name or g_name
                    detail_parts = []
                    if member_name:    detail_parts.append(f"Member: {member_name}")
                    if branch:         detail_parts.append(f"Branch: {branch}")
                    if state:          detail_parts.append(f"State: {state}")
                    if d_name:         detail_parts.append(
                        f"Director: {d_name}" + (f" (DIN {din})" if din else ""))
                    if d_pan:          detail_parts.append(f"Director PAN: {d_pan}")
                    if g_name:         detail_parts.append(f"Guarantor: {g_name}")
                    if g_cin:          detail_parts.append(f"Guarantor CIN: {g_cin}")
                    if g_pan:          detail_parts.append(f"Guarantor PAN: {g_pan}")
                    if suit_status:    detail_parts.append(f"Suit Status: {suit_status}")
                    if cycle:          detail_parts.append(f"Reporting Cycle: {cycle}")
                    key = (name.lower(), b_pan, d_name.lower(), g_name.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "source_agency": "Bank of Baroda (BOB)",
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
    print("Bank of Baroda Wilful Defaulters (#130)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("BOB: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
