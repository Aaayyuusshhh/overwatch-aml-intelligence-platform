"""
Income Tax Tax Defaulters (#35).

Source: https://www.incometaxindia.gov.in/tax-defaulters

The page is a Liferay portal stub. The actual defaulter list is
served by a Liferay Headless Delivery endpoint:
  https://www.incometaxindia.gov.in/o/c/taxdefaulterses?pageSize=10000

Each item is a JSON object with name, PAN, address, tax-arrear amount,
date of birth / incorporation, source-of-income, ITA office, etc.
The list currently has 96 entries.
"""

import csv
import os
import re
from datetime import datetime

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_URL = ("https://www.incometaxindia.gov.in/o/c/taxdefaulterses"
           "?pageSize=10000")
DETAIL_BASE = "https://www.incometaxindia.gov.in/tax-defaulters"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "income_tax_defaulters_35.csv")

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
      "Accept": "application/json"}


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _extract_father(raw):
    """The fathersDirectorsPartnersName field uses prose like
    "FATHER'S NAME: - KANTILAL SHAH" or "FATHER'S NAME:- X".
    Return just the name where extractable."""
    if not raw:
        return ""
    s = _clean(raw)
    m = re.search(r"FATHER'?S\s+NAME\s*:?\s*-?\s*(.+)$", s, re.I)
    if m:
        return _clean(m.group(1)).rstrip(".,;: -")
    return s


def scrape():
    r = requests.get(API_URL, headers=UA, timeout=60, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"IT Defaulters: API returned {r.status_code}")
    payload = r.json()
    items = payload.get("items") or []
    total = payload.get("totalCount")
    print(f"  fetched {len(items)} records (totalCount={total})")
    if items and total and len(items) < total:
        raise RuntimeError(
            f"IT Defaulters: paging cut off at {len(items)}/{total}"
        )
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for it in items:
        name = _clean(it.get("name") or it.get("name_i18n", {}).get("en_US"))
        if not name:
            continue
        pan = _clean(it.get("pan"))
        amount = it.get("taxArrear")
        amount_str = f"{amount}" if amount is not None else ""
        address = _clean(it.get("lastKnownAddress")
                          or it.get("lastKnownAddress_i18n", {}).get("en_US"))
        father = _extract_father(it.get("fathersDirectorsPartnersName"))
        dob_raw = _clean(it.get("dateOfBirthDateOfIncorporation"))
        # Convert ISO timestamp to YYYY-MM-DD when possible.
        dob = ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", dob_raw)
        if m:
            dob = m.group(1)
        ay = _clean(it.get("assessmentYear"))
        category = _clean(it.get("categoryOfAssessee"))
        source = _clean(it.get("lastKnownSourceOfIncome"))
        authority = _clean(it.get("incomeTaxAuthorityRawText")
                            or it.get("incomeTaxAuthority"))
        remarks = _clean(it.get("remarks"))
        details = " | ".join(p for p in [
            f"PAN: {pan}" if pan else "",
            (f"Tax Arrear: ₹{amount} lakhs"
                if amount is not None else ""),
            f"Assessment Year: {ay}" if ay else "",
            f"Category: {category}" if category else "",
            f"Source of Income: {source}" if source else "",
            f"Income Tax Authority: {authority}" if authority else "",
            f"Remarks: {remarks}" if remarks else "",
        ] if p)
        out.append({
            "source_agency": "Income Tax Department",
            "source_list": "Tax Defaulters",
            "case_unit": pan,
            "name": name,
            "father_name": father,
            "date_of_birth": dob,
            "gender": "",
            "address": address,
            "reward_amount": amount_str,
            "details": details,
            "has_document": "No",
            "document_url": "",
            "detail_page_url": DETAIL_BASE,
            "interpol_notice_id": "",
            "link_kind": "url_discovery",
            "scraped_at": scraped_at,
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
    print("Income Tax Tax Defaulters (#35)")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
