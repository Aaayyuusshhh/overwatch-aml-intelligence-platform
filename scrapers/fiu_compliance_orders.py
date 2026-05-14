"""
FIU Compliance Orders + Judgements (bonus source, no PPT number).

Source: https://fiuindia.gov.in/files/Compliance_Orders/orders.html

The page has two tab panels — Orders and Judgements:

  Orders: 13 tables, one per year (2025 -> 2013, newest-first).
    Columns: S.No | Date | Description | Document Size | Document(PDF link)

  Judgements: 4 entries (2016: 1, 2015: 3) as <li class="listbullet">
    each containing an <a href> with text "<Name> Dated:<date>".

We split the Description into entity name and the rest:
  "Bybit Fintech Limited Order in original No. 15/DIR/FIU-IND/2024 u/s …"
    -> name = "Bybit Fintech Limited"
  "Bonanza Portfolio Limited u/s Section 13,Order-in-Original No. 14/…"
    -> name = "Bonanza Portfolio Limited"
The entity precedes the first occurrence of "Order", "u/s", or ",Order".
"""

import csv
import os
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://fiuindia.gov.in/files/Compliance_Orders/orders.html"
PDF_BASE = "https://fiuindia.gov.in/"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "fiu_compliance_orders.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# Years run newest -> oldest in the HTML; table index N corresponds to
# year (2025 - N).
TABLE_YEAR_OFFSET = 2025

# Splits a Description into entity name + the rest. Anchors on the
# first occurrence of "Order", ",Order", "u/s", or "Dated".
_DESC_SPLIT = re.compile(
    r"(?:[\s,]+(?:Order|u/s|Dated\b|\(reporting\s+entity\)))",
    re.I,
)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _split_description(desc):
    """Return (entity_name, rest_of_description)."""
    desc = _clean(desc)
    if not desc:
        return "", ""
    m = _DESC_SPLIT.search(desc)
    if not m:
        return desc, ""
    return desc[: m.start()].strip(" .,;-"), desc[m.start():].strip()


def _fetch_html():
    """Fetch the orders page; fall back to /tmp cache if WAF returns the
    246-byte rejection page."""
    r = Fetcher.get(LIST_URL, timeout=60, retries=2, retry_delay=2,
                    verify=False)
    body = getattr(r, "body", None) or getattr(r, "content", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    if not body or "Request Rejected" in body or len(body) < 2000:
        raise RuntimeError(
            f"FIU Compliance Orders: fetch blocked (len={len(body or '')})"
        )
    return body


def _parse_orders(soup, scraped_at):
    rows = []
    tables = soup.find_all("table")
    for idx, t in enumerate(tables):
        year = TABLE_YEAR_OFFSET - idx
        trs = t.find_all("tr")
        for tr in trs[1:]:  # skip header
            cells = tr.find_all(["td", "th"])
            if len(cells) < 5:
                continue
            sn = _clean(cells[0].get_text(" ", strip=True))
            date = _clean(cells[1].get_text(" ", strip=True))
            desc = _clean(cells[2].get_text(" ", strip=True))
            size = _clean(cells[3].get_text(" ", strip=True))
            # Document column: extract <a href> if present
            link = cells[4].find("a", href=True)
            doc_url = urljoin(LIST_URL, link["href"]) if link else ""
            if not desc:
                continue
            entity, rest = _split_description(desc)
            if not entity:
                continue
            detail_parts = [
                f"Year: {year}",
                f"Date: {date}" if date else "",
                rest,
                f"Document size: {size}" if size else "",
            ]
            rows.append({
                "source_agency": "Financial Intelligence Unit (FIU)",
                "source_list": "Compliance Orders",
                "case_unit": "",
                "name": entity,
                "father_name": "",
                "date_of_birth": "",
                "gender": "",
                "address": "",
                "reward_amount": "",
                "details": " | ".join(p for p in detail_parts if p),
                "has_document": "Yes" if doc_url else "No",
                "document_url": doc_url,
                "detail_page_url": LIST_URL,
                "interpol_notice_id": "",
                "link_kind": "manual_discovery",
                "scraped_at": scraped_at,
                "enrichment_status": "",
            })
    return rows


def _parse_judgements(soup, scraped_at):
    """Find the Judgements panel: years in <p class="statements"> and
    items in <li class="listbullet"><a href>…</a></li>."""
    out = []
    # The judgement section lives in the same DOM; find the <li> entries
    # that have class "listbullet" — those are the 4 judgement bullets.
    bullets = soup.find_all("li", class_="listbullet")
    for li in bullets:
        a = li.find("a", href=True)
        if not a:
            continue
        text = _clean(a.get_text(" ", strip=True))
        # Format: "Allahabad Bank Ltd. Dated:22nd September 2015"
        m = re.match(r"(?P<name>.+?)\s*Dated\s*:\s*(?P<date>.+)$",
                     text, re.I)
        if not m:
            continue
        name = m.group("name").strip(" .,;-")
        date = m.group("date").strip()
        doc_url = urljoin(LIST_URL, a["href"])
        out.append({
            "source_agency": "Financial Intelligence Unit (FIU)",
            "source_list": "Judgements",
            "case_unit": "",
            "name": name,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": "",
            "reward_amount": "",
            "details": f"Date: {date}",
            "has_document": "Yes",
            "document_url": doc_url,
            "detail_page_url": LIST_URL,
            "interpol_notice_id": "",
            "link_kind": "manual_discovery",
            "scraped_at": scraped_at,
            "enrichment_status": "",
        })
    return out


def scrape():
    html = _fetch_html()
    soup = BeautifulSoup(html, "html.parser")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    orders = _parse_orders(soup, scraped_at)
    judgements = _parse_judgements(soup, scraped_at)
    print(f"  parsed {len(orders)} orders + {len(judgements)} judgements")
    return orders + judgements


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
    print("FIU Compliance Orders + Judgements")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
