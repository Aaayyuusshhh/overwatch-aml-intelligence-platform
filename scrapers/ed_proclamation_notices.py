"""
ED Notice For Proclamation (#24).

Source: https://enforcementdirectorate.gov.in/wanted/notice-for-proclamation/
The visible page is a stub; the actual data is served by the Umbraco
Notice Manager API:
  GET https://enforcementdirectorate.gov.in/umbraco/backoffice/api/NoticeManagerApi/GetPublic?language=en&type=proclamation

Response: list[dict] with Id, Title, Description, PdfUrl, CreatedDate,
Language. The accused-person name is in the Title in patterns like
"… vs <NAME> (case-no) against <NAME> S/o <FATHER> …" — we extract
between "against " and the following " S/o " or "to appear" / "in
the case".
"""

import csv
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_URL = ("https://enforcementdirectorate.gov.in/umbraco/backoffice/api/"
           "NoticeManagerApi/GetPublic?language=en&type=proclamation")
DETAIL_PAGE = ("https://enforcementdirectorate.gov.in/wanted/"
               "notice-for-proclamation/")
ORIGIN = "https://enforcementdirectorate.gov.in/"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "ed_proclamation_notices_24.csv")

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

# "against <NAME> S/o" / "against <NAME>, S/o" / "against <NAME> to appear"
AGAINST_RE = re.compile(
    r"\bagainst\s+(?P<name>.+?)\s*"
    r"(?:[, ]\s*[Ss]/o\s+(?P<father>[^,()]+?)\b|to\s+appear|in\s+the\s+(?:case|matter)|\(|,\s*for\b|\bunder\b)",
    re.I,
)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" .,;-")


def _extract(title):
    """Return (name, father_name)."""
    m = AGAINST_RE.search(title or "")
    if not m:
        return "", ""
    name = _clean(m.group("name"))
    father = _clean(m.group("father")) if m.lastgroup else ""
    return name, father


def scrape():
    r = requests.get(API_URL, headers=UA, timeout=30, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"ED Proclamation: status {r.status_code}")
    items = r.json()
    print(f"  fetched {len(items)} proclamation notices")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for it in items:
        title = _clean(it.get("Title"))
        desc = _clean(it.get("Description"))
        name, father = _extract(title or desc)
        if not name:
            # Fall back to first 80 chars of title for inspection
            name = title[:80] if title else ""
        if not name:
            continue
        pdf = (it.get("PdfUrl") or "").strip()
        if pdf and not pdf.startswith("http"):
            pdf = urljoin(ORIGIN, pdf)
        out.append({
            "source_agency": "Directorate of Enforcement (ED)",
            "source_list":   "Notice For Proclamation",
            "case_unit":     str(it.get("Id") or ""),
            "name":          name,
            "father_name":   father,
            "date_of_birth": "",
            "gender":        "",
            "address":       "",
            "reward_amount": "",
            "details":       (f"Date: {it.get('CreatedDate','')} | "
                              f"Description: {desc[:400]}").strip(),
            "has_document":  "Yes" if pdf else "No",
            "document_url":  pdf,
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
    print("ED Notice For Proclamation (#24)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("ED Proclamation: 0 rows")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
