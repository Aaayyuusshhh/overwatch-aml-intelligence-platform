"""
Jharkhand Police List of Rewarded Naxal/Criminals scraper (#225).

Source: https://jhpolice.gov.in/rewarded-naxal
The page has one HTML table with Hindi headers:
  क्र0सं0 (Sr.No) | नक्सली का नाम एवं पता (Name+Address) | संगठन (Org)
  | पद (Role) | घोषित पुरस्कार राशि (Reward)
The generic engine misses the column mapping because none of its
English NAME_HEADERS match. Custom scraper hard-codes column 1 = name.
"""

import csv
import os
import re
from datetime import datetime

from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://jhpolice.gov.in/rewarded-naxal"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "jp_list_of_rewarded_naxal_225.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _clean(s):
    if s is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", s).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def scrape():
    r = Fetcher.get(LIST_URL, timeout=45, retries=1, retry_delay=0, verify=False)
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")

    # Find largest table.
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", body, re.I)
    best, best_rows = None, []
    for t in tables:
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
        if len(trs) > len(best_rows):
            best, best_rows = t, trs
    if not best_rows:
        raise RuntimeError("Jharkhand: no table found")

    parsed = []
    for tr in best_rows:
        cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)
        parsed.append([_clean(c) for c in cells])

    # Skip rows that are title (1 cell) or header (matches 'क्र0सं0' / 'नाम').
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for cells in parsed:
        if len(cells) < 4:
            continue
        sno = cells[0]
        name_addr = cells[1]
        org = cells[2] if len(cells) > 2 else ""
        role = cells[3] if len(cells) > 3 else ""
        reward = cells[4] if len(cells) > 4 else ""
        # Header row?
        if "नाम" in name_addr or "क्र" in sno or "नक्सली" in name_addr.lower() and "पता" in name_addr:
            continue
        if not name_addr:
            continue
        # Heuristic: the name and address are concatenated. Often there's
        # a comma or "ग्राम" word splitting the two. We keep the whole
        # blob as 'name' and re-extract a likely address suffix.
        # Examples: "मिसिर बेसरा उर्फ भास्कर ..., पिता- दर्पण भास्कर ग्राम मदनडीह, थाना-पीरटांड़ जिला- गिरिडीह।"
        addr = ""
        m = re.search(r"(ग्राम\s*[-–]?\s*.*?(?:जिला|जिल्ला)\s*[-–]?\s*\S+)", name_addr)
        if m:
            addr = m.group(1)
            name_short = name_addr[:m.start()].rstrip(", ")
        else:
            name_short = name_addr
        out.append({
            "source_agency": "Jharkhand Police",
            "source_list": "List of Rewarded Naxal/Criminals",
            "case_unit": sno,
            "name": name_short,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": addr,
            "reward_amount": reward,
            "details": " | ".join(p for p in (
                f"Org: {org}" if org else "",
                f"Role: {role}" if role else "",
                f"S.No: {sno}" if sno else "",
            ) if p),
            "has_document": "No",
            "document_url": "",
            "detail_page_url": LIST_URL,
            "interpol_notice_id": "",
            "link_kind": "jharkhand_rewarded_naxal",
            "scraped_at": scraped_at,
            "enrichment_status": "none",
        })
    return out


def save_to_csv(rows, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {out_path}")


def run():
    print("=" * 60)
    print("Jharkhand Police - Rewarded Naxal/Criminals scraper (#225)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("Jharkhand: zero rows extracted")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
