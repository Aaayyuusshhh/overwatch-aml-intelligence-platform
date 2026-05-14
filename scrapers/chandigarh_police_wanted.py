"""
Chandigarh Police — Wanted Persons.

Two PDFs on chandigarhpolice.gov.in carry wanted-person data:

  pdf/REWARD_ON_WANTED PERSON.pdf
    PUBLIC NOTICE about FIR No. 63/2023 (PS Sector-36) — 7 accused
    listed with names + addresses in a 4-table photo-grid layout.
    Reward Rs 10,000 for identity info.

  pdf/Wanted_Person_EOW.pdf
    Wanted persons by Economic Offence Wing, Sector 17, Chandigarh —
    7 accused listed in a 4-column table (Photo / Particulars /
    Wanted by / Announced Reward). The wanted_person_by_EOW.pdf is
    an older copy of the same list, skipped.
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
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "chandigarh_police_wanted.csv")

REWARD_PDF_URL = "https://chandigarhpolice.gov.in/pdf/REWARD_ON_WANTED PERSON.pdf"
EOW_PDF_URL    = "https://chandigarhpolice.gov.in/pdf/Wanted_Person_EOW.pdf"

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
      "Accept": "*/*"}


def _clean(s):
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip(" .,;-")
    return s


def _ensure_pdf(url, local_name):
    path = os.path.join(RAW_DIR, local_name)
    if os.path.exists(path) and os.path.getsize(path) > 50_000:
        return path
    os.makedirs(RAW_DIR, exist_ok=True)
    r = requests.get(url, headers=UA, timeout=60, verify=False)
    if r.status_code != 200 or not r.content[:8].lstrip().startswith(b"%PDF"):
        raise RuntimeError(f"Chandigarh: failed to fetch {url}")
    with open(path, "wb") as f:
        f.write(r.content)
    return path


# ----- PDF 1: PUBLIC NOTICE (FIR 63/2023) ----------------------------------
SO_RE = re.compile(r"\s+S/o\s+", re.I)
RO_RE = re.compile(r"\bR/O\s+", re.I)

# The names in this PDF appear concatenated in cell text like
# "HARDEEP SINGH\nBRAR", "HARMANDEEP SINGH\nTUFAN" — they're given names
# split across lines. We use the page's full text to recover them
# rather than the per-table cells, which lose row alignment.
PUBLIC_NOTICE_NAMES = re.compile(
    r"([A-Z][A-Z\s]+(?:@\s*[A-Z][A-Z\s]+)?(?:@\s*[A-Z][A-Z\s]+)?)"
    r"\s+R/O\s+([A-Z][A-Z\s,]+?)(?=\s*(?:[A-Z]{3,}\s+R/O|\Z))",
    re.I,
)


def _parse_reward_pdf(path, scraped_at):
    """Walk pdfplumber tables cell-by-cell. Each non-empty cell on
    pages of the public-notice PDF holds one accused person —
    name on one line, alias on another (separated by '@'), R/O <addr>
    on a trailing line."""
    out = []
    seen = set()
    fir = "FIR No. 63/2023, PS Sector-36, Chandigarh"
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    for cell in row:
                        if not cell:
                            continue
                        text = cell.replace("\xa0", " ").strip()
                        if len(text) < 5:
                            continue
                        # Skip the preamble cells.
                        low = text.lower()
                        if any(skip in low for skip in
                               ("public notice", "general public",
                                "informer will", "identity will",
                                "following accused", "email id-")):
                            continue
                        # Each cell of interest contains uppercase Latin
                        # words; reject any cell that's mostly other
                        # script.
                        if not re.search(r"[A-Z]{3,}", text):
                            continue
                        # Split on R/O to separate name+aliases from
                        # address.
                        m = re.split(r"\bR/O\s+", text, maxsplit=1, flags=re.I)
                        name_part = re.sub(r"\s+", " ", m[0]).strip(" .,;-")
                        address   = (re.sub(r"\s+", " ",
                                            m[1]).strip(" .,;-")
                                     if len(m) > 1 else "")
                        # Drop generic "BRAR" / "SANDHU" surname-only
                        # cells (often appear as standalone fragments
                        # in this layout).
                        if len(name_part.split()) < 2 and "@" not in name_part:
                            continue
                        key = name_part.upper()
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({
                            "source_agency": "Chandigarh Police (CP)",
                            "source_list":   "Wanted Persons",
                            "case_unit":     "FIR 63/2023",
                            "name":          name_part,
                            "father_name":   "",
                            "date_of_birth": "",
                            "gender":        "",
                            "address":       address,
                            "reward_amount": "10000",
                            "details":       (f"Source: {fir} | "
                                              "Reward: Rs 10,000 for identity info | "
                                              "Contact: firno.63@gmail.com / 9875984001"),
                            "has_document":  "Yes",
                            "document_url":  REWARD_PDF_URL,
                            "detail_page_url": "https://chandigarhpolice.gov.in/most-wanted.html",
                            "interpol_notice_id": "",
                            "link_kind":     "manual_discovery",
                            "scraped_at":    scraped_at,
                            "enrichment_status": "",
                        })
    return out


# ----- PDF 2: EOW Wanted ---------------------------------------------------
def _parse_eow_pdf(path, scraped_at):
    out = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [_clean(c) for c in (row + [""] * 4)[:4]]
                    _photo, particulars, wanted_by, reward = cells
                    if not particulars or particulars.lower() == "particulars of wanted person":
                        continue
                    # particulars is multi-line: "<Name> S/o <Father>\nR/o <Address>\n<Official Address...>"
                    text = particulars.replace("\n", " ")
                    # name = text before "S/o"
                    m = re.match(r"(.+?)\s+S/o\s+([^,]+?)(?=\s+R/o|\Z)", text, re.I)
                    if m:
                        name = _clean(m.group(1))
                        father = _clean(m.group(2))
                    else:
                        name = _clean(text.split(" R/o ")[0]) if " R/o " in text else _clean(text)
                        father = ""
                    addr_m = re.search(r"\bR/o\s+(.+?)$", text, re.I)
                    address = _clean(addr_m.group(1)) if addr_m else ""
                    if not name:
                        continue
                    # reward like "Rs. 50,000/-" → just digits
                    reward_n = re.sub(r"[^\d]", "", reward)
                    out.append({
                        "source_agency": "Chandigarh Police (CP)",
                        "source_list":   "Wanted Persons",
                        "case_unit":     "",
                        "name":          name,
                        "father_name":   father,
                        "date_of_birth": "",
                        "gender":        "",
                        "address":       address,
                        "reward_amount": reward_n,
                        "details":       (f"Source: Economic Offence Wing, Sector 17 | "
                                          f"Wanted by: {wanted_by} | "
                                          f"Announced Reward: {reward}"),
                        "has_document":  "Yes",
                        "document_url":  EOW_PDF_URL,
                        "detail_page_url": "https://chandigarhpolice.gov.in/most-wanted.html",
                        "interpol_notice_id": "",
                        "link_kind":     "manual_discovery",
                        "scraped_at":    scraped_at,
                        "enrichment_status": "",
                    })
    return out


def scrape():
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reward_pdf = _ensure_pdf(REWARD_PDF_URL, "chandigarh_reward_on_wanted.pdf")
    eow_pdf    = _ensure_pdf(EOW_PDF_URL,    "chandigarh_wanted_eow.pdf")
    a = _parse_reward_pdf(reward_pdf, scraped_at)
    b = _parse_eow_pdf(eow_pdf, scraped_at)
    print(f"  reward-pdf: {len(a)} | eow-pdf: {len(b)}")
    # dedup across PDFs by (name lowercase)
    seen = set()
    out = []
    for r in a + b:
        key = r["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
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
    print("Chandigarh Police — Wanted Persons")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("Chandigarh Police: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
