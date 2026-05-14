"""
Karnataka State Police — Wanted Persons (#214).

Source: https://ksp.karnataka.gov.in/new-page/Wanted/en

The page links 3 PDFs:

  1. /storage/pdf-files/Wanted_TelanganaState.pdf  — 72-page
     consolidated wanted/absconder roster shared by Telangana
     CID. English text. Names appear in case narratives following
     the "<Name> S/o|D/o|W/o <Father>" pattern.
  2. /storage/pdf-files/IGP_B_Range_LookOut.pdf    — 2-page
     letter from DCRB Ballari circulating one absconder
     ("Veeresh @ Suresh S/o Korachara Nagappa"). One name.
  3. /storage/pdf-files/ccbWanted.pdf              — Kannada-only,
     pdfplumber returns unmapped glyphs. Skipped.

We emit one record per unique (name) tuple. Father's name carried
in `father_name`. Source PDF URL goes into document_url.
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
LIST_URL = "https://ksp.karnataka.gov.in/new-page/Wanted/en"
PDF_URLS = {
    "telangana": "https://ksp.karnataka.gov.in/storage/pdf-files/Wanted_TelanganaState.pdf",
    "igp_b":     "https://ksp.karnataka.gov.in/storage/pdf-files/IGP_B_Range_LookOut.pdf",
}
CACHE_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "karnataka_police_wanted_214.csv")

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
      "Referer": "https://ksp.karnataka.gov.in/"}

# "<Name [@ alias]> S/o|D/o|W/o <Father>" followed by age/Yrs/R-o/Age boundary
NAME_PAT = re.compile(
    r"((?:[A-Z][A-Za-z]+(?:\s+@\s+\w+)?\s+){1,5})"
    r"(?:S/o|D/o|W/o|s/o|d/o|w/o)"
    r"\s+([A-Z][A-Za-z\s]+?)"
    r"(?:,|\s+\d+\s*[Yy]r|\s+Age\b|\s+R/o|\s+r/o)",
    re.MULTILINE,
)

# Reject father-name matches that bleed into case-section text
_BAD_FATHER = re.compile(r"\b(IPC|Sec|Cr\.?No|PS|R/o|U/s|Night|Late)\b", re.I)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" ,;.-")


def _fetch_pdf(url, tag):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"ksp_{tag}.pdf")
    if os.path.exists(path) and os.path.getsize(path) > 100_000:
        return path
    r = requests.get(url, headers=UA, timeout=120, verify=False)
    if r.status_code != 200 or r.content[:4] != b"%PDF":
        raise RuntimeError(f"KSP {tag}: download failed (status={r.status_code})")
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def _names_from_pdf(path):
    """Walk pages, extract normalised text, find S/o-pattern names."""
    out = []
    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            text = re.sub(r"\s+", " ", text)
            for name_m, father_m in NAME_PAT.findall(text):
                name = _clean(name_m)
                father = _clean(father_m)
                if not name or len(name.split()) < 2:
                    continue
                if _BAD_FATHER.search(father):
                    continue
                out.append((name, father, pi))
    return out


def scrape():
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()

    # Telangana — 72-page consolidated roster
    tel_pdf = _fetch_pdf(PDF_URLS["telangana"], "telangana")
    for name, father, pi in _names_from_pdf(tel_pdf):
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source_agency": "Karnataka State Police (KSP)",
            "source_list":   "Wanted Persons",
            "case_unit":     "",
            "name":          name,
            "father_name":   father,
            "date_of_birth": "",
            "gender":        "",
            "address":       "",
            "reward_amount": "",
            "details":       f"Source: Wanted_TelanganaState.pdf page {pi + 1}",
            "has_document":  "Yes",
            "document_url":  PDF_URLS["telangana"],
            "detail_page_url": LIST_URL,
            "interpol_notice_id": "",
            "link_kind":     "manual_discovery",
            "scraped_at":    scraped_at,
            "enrichment_status": "",
        })

    # IGP_B Range LookOut — letter, single subject
    igp_pdf = _fetch_pdf(PDF_URLS["igp_b"], "igp_b")
    with pdfplumber.open(igp_pdf) as pdf:
        full = " ".join(p.extract_text() or "" for p in pdf.pages)
        full = re.sub(r"\s+", " ", full)
    m = re.search(r"Sub-\s*Circulating of information of\s+([A-Z][^,]+?),", full)
    if m:
        subject = _clean(m.group(1))
        # The subject is "Veeresh @ Suresh" — keep as-is
        key = subject.lower()
        if subject and key not in seen:
            seen.add(key)
            out.append({
                "source_agency": "Karnataka State Police (KSP)",
                "source_list":   "Wanted Persons",
                "case_unit":     "",
                "name":          subject,
                "father_name":   "",
                "date_of_birth": "",
                "gender":        "",
                "address":       "",
                "reward_amount": "",
                "details":       "Source: IGP_B_Range_LookOut.pdf | District: Ballari",
                "has_document":  "Yes",
                "document_url":  PDF_URLS["igp_b"],
                "detail_page_url": LIST_URL,
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
    print("Karnataka Police Wanted Persons (#214)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("KSP: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
