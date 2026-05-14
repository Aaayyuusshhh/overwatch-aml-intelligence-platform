"""
IRDAI Warnings & Penalties (a.k.a. Enforcement Orders).

Source: https://irdai.gov.in/enforcement → redirects to
        https://irdai.gov.in/warnings-and-penalties

The page is rendered by a Liferay portlet
`com_irdai_document_media_IRDAIDocumentMediaPortlet`. Pagination is
driven by `delta=<page-size>` and `cur=<page-no>` parameters scoped
to that portlet name. At time of scrape there are 403 records across
~7 pages with delta=60.

Many records carry Hindi short-descriptions and titles; we preserve
them as-is (Devanagari) but strip honorifics for cleaner screening.
"""

import csv
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "irdai_enforcement.csv")

BASE = ("https://irdai.gov.in/warnings-and-penalties"
        "?p_p_id=com_irdai_document_media_IRDAIDocumentMediaPortlet"
        "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
        "&_com_irdai_document_media_IRDAIDocumentMediaPortlet_delta=60")
DETAIL_PAGE = "https://irdai.gov.in/warnings-and-penalties"

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
      "Accept": "text/html"}

HONORIFICS = re.compile(r"^\s*(?:श्री|श्रीमती|कुमारी|Mr\.?|Mrs\.?|Ms\.?|"
                          r"Shri\.?|Smt\.?|M/s\.?)\s+", re.I)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" .,;-")


def _strip_honorific(s):
    prev = None
    while s and s != prev:
        prev = s
        s = HONORIFICS.sub("", s).strip()
    return s


def scrape():
    sess = requests.Session()
    sess.headers.update(UA)
    sess.verify = False
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    for cur in range(1, 12):                  # safety: 7 pages expected, cap at 11
        url = f"{BASE}&_com_irdai_document_media_IRDAIDocumentMediaPortlet_cur={cur}"
        r = sess.get(url, timeout=45)
        if r.status_code != 200:
            print(f"  cur={cur}: status {r.status_code}")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        t = soup.find("table")
        if not t:
            break
        rows = t.find_all("tr")
        added = 0
        for tr in rows:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 7:
                continue
            # Layout: ['', archive, short-desc, last-updated, sub-title, ref-no, documents]
            archive    = _clean(cells[1].get_text(" ", strip=True))
            short_desc = _clean(cells[2].get_text(" ", strip=True))
            last_upd   = _clean(cells[3].get_text(" ", strip=True))
            sub_title  = _clean(cells[4].get_text(" ", strip=True))
            ref_no     = _clean(cells[5].get_text(" ", strip=True))
            docs_cell  = cells[6]
            # Skip header
            if archive.lower() in {"archive / non archive", "archive/non archive"}:
                continue
            if not short_desc:
                continue
            doc_link = docs_cell.find("a", href=True)
            doc_url  = urljoin(url, doc_link["href"]) if doc_link else ""
            # `short_desc` is the entity-bearing title. Strip leading
            # honorifics for cleaner screening.
            name = _strip_honorific(short_desc)
            # Some short_desc texts are truncated with "..."; keep them
            # as-is — partial name is better than no name.
            if not name:
                continue
            key = (name.lower()[:120], ref_no.lower())
            if key in seen:
                continue
            seen.add(key)
            detail_bits = []
            if ref_no:    detail_bits.append(f"Ref: {ref_no}")
            if sub_title and sub_title.lower() != short_desc.lower():
                detail_bits.append(f"Sub-title: {sub_title}")
            if last_upd:  detail_bits.append(f"Updated: {last_upd}")
            if archive:   detail_bits.append(f"Archive: {archive}")
            out.append({
                "source_agency": "Insurance Regulatory and Development Authority of India (IRDAI)",
                "source_list":   "Warnings and Penalties",
                "case_unit":     ref_no,
                "name":          name,
                "father_name":   "",
                "date_of_birth": "",
                "gender":        "",
                "address":       "",
                "reward_amount": "",
                "details":       " | ".join(detail_bits),
                "has_document":  "Yes" if doc_url else "No",
                "document_url":  doc_url,
                "detail_page_url": DETAIL_PAGE,
                "interpol_notice_id": "",
                "link_kind":     "recon_discovery",
                "scraped_at":    scraped_at,
                "enrichment_status": "",
            })
            added += 1
        print(f"  cur={cur}: +{added}  total={len(out)}")
        time.sleep(2.0)
        if added == 0:
            break
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
    print("IRDAI Warnings & Penalties (Enforcement)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("IRDAI: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
