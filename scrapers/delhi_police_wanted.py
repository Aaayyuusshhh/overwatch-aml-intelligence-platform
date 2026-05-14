"""
Delhi Police Wanted Criminals (#209).

Source: https://delhipolice.ncog.gov.in/Delhi_police/wantedcriminals.html

The page is a small static HTML (~8 KB) that lists:
  * 3 named Pakistani-terrorist wanteds (₹50,000 reward each) — with
    photo and a <p>NAME</p> caption next to each <img>.
  * 9 "Wanted Criminals in Rape case" with photos and an RC-number
    caption (no names — CBI is investigating). We capture the
    RC numbers as case_unit so screening on the RC works, and use
    the RC as the canonical name (otherwise unusable rows).
  * 1 EOW criminal (Ravinder Jain) with full bio in a single <p>.
  * Year-link DOC files (2013-2018) — text-encoded, hard to parse
    cleanly from script. Recorded as document_url references rather
    than parsed inline.
"""

import csv
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://delhipolice.ncog.gov.in/Delhi_police/wantedcriminals.html"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "delhi_police_wanted_209.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/120.0.0.0 Safari/537.36"}


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" .,;-")


def _record(name, **fields):
    base = {
        "source_agency": "Delhi Police (DP)",
        "source_list":   "Wanted Criminals",
        "case_unit":     "",
        "name":          name,
        "father_name":   "",
        "date_of_birth": "",
        "gender":        "",
        "address":       "",
        "reward_amount": "",
        "details":       "",
        "has_document":  "No",
        "document_url":  "",
        "detail_page_url": LIST_URL,
        "interpol_notice_id": "",
        "link_kind":     "manual_discovery",
        "scraped_at":    "",
        "enrichment_status": "",
    }
    base.update(fields)
    return base


# ---- bio extraction for the EOW long entry -------------------------------
def _parse_eow_bio(text):
    name = father = address = court = date_po = ""
    m = re.search(r"NAME\s*:\s*([^\n<]+?)(?=\s*FATHER\s+NAME\b|$)", text, re.I)
    if m: name = _clean(m.group(1))
    m = re.search(r"FATHER\s+NAME\s*:\s*([^\n<]+?)(?=\s*ADDRESS\b|$)", text, re.I)
    if m: father = _clean(m.group(1))
    m = re.search(r"ADDRESS\s*:\s*([^\n<]+?)(?=DATE\s+OF\s+DECLARATION|$)", text, re.I)
    if m: address = _clean(m.group(1))
    m = re.search(r"DATE\s+OF\s+DECLARATION\s+OF\s+PO\s*:\s*([^\n<]+?)(?=NAME\s+OF\s+THE\s+COURT|$)", text, re.I)
    if m: date_po = _clean(m.group(1))
    m = re.search(r"NAME\s+OF\s+THE\s+COURT\s*:\s*([^\n<]+?)$", text, re.I)
    if m: court = _clean(m.group(1))
    return name, father, address, date_po, court


def scrape():
    r = requests.get(LIST_URL, headers=UA, timeout=30, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"Delhi Police Wanted: status {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []

    # 1. Three Pakistani terrorists — pattern: <img src="wc/wcN.jpg"...><p> NAME </p>
    for img in soup.find_all("img"):
        p = img.find_next("p")
        if not p:
            continue
        text = _clean(p.get_text(" ", strip=True))
        if not text:
            continue
        # If text starts with "RC No" it's a rape-case rc — handled below.
        if text.upper().startswith("RC NO"):
            rc = text
            out.append(_record(
                name=f"Unknown wanted ({rc})",
                case_unit=rc,
                details=(f"Category: Wanted in Rape case (CBI/SC.III/ND) | "
                          f"Photo: {urljoin(LIST_URL, img.get('src',''))}"),
                has_document="Yes",
                document_url=urljoin(LIST_URL, img.get("src", "")),
                scraped_at=scraped_at,
            ))
            continue
        # Else this is a Pakistani-terrorist named caption.
        # The first 3 named entries carry a 50,000 reward.
        photo = urljoin(LIST_URL, img.get("src", ""))
        out.append(_record(
            name=text,
            reward_amount="50000",
            details=(f"Category: Wanted Pakistani Terrorist | "
                      f"Reward: Rs. 50,000 | Photo: {photo}"),
            has_document="Yes",
            document_url=photo,
            scraped_at=scraped_at,
        ))

    # 2. EOW long bio
    eow_section = soup.find(string=re.compile(r"NAME\s*:.*FATHER\s+NAME", re.I))
    if eow_section:
        text = eow_section.parent.get_text(" ", strip=True)
        nm, fa, addr, dpo, court = _parse_eow_bio(text)
        if nm:
            out.append(_record(
                name=nm, father_name=fa, address=addr,
                details=(f"Category: Wanted Criminals of EOW | "
                          f"Date of PO declaration: {dpo} | "
                          f"Court: {court}").strip(" |"),
                scraped_at=scraped_at,
            ))

    # 3. DOC reference rows for the year-link bundles (unparsed but
    # surfaced so analysts have the source PDF/DOC for manual review).
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = _clean(a.get_text(" ", strip=True))
        if not href.lower().endswith((".doc", ".docx")):
            continue
        out.append(_record(
            name=f"Delhi Police bundle: {text}",
            details=(f"Category: Wanted-criminals bundled DOC file. "
                      f"Contains multiple reward entries; raw URL "
                      f"recorded for analyst review."),
            has_document="Yes",
            document_url=href,
            scraped_at=scraped_at,
            link_kind="manual_discovery",
        ))
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
    print("Delhi Police Wanted Criminals (#209)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("Delhi Police Wanted: 0 rows")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
