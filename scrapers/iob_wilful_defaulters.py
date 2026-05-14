"""
IOB Wilful & Large Defaulters scraper (#134).

Source: https://www.iob.bank.in/en/customers-care
The page links four XLSX files (Wilful + Large, Suit-filed + Non-suit-filed).
Each XLSX has 17 columns: Reporting Cycle, Member ID, Member Name,
Branch, State, Borrower Name, Borrower PAN, Borrower Address, Outstanding
(Lakhs), Suit Status, Other Member, Director/Promoter Name, DIN,
Director PAN, Guarantor Name, Guarantor CIN, Guarantor PAN. Rows repeat
per director/guarantor for the same borrower.

We map each XLSX row to one watchlist record where:
  name = Borrower Name (the entity in default)
  case_unit = Borrower PAN
  address = Borrower Address
  reward_amount = Outstanding (Lakhs)
  details = pipe-joined Director/Guarantor/Branch/State data
  source_list = derived from filename (Wilful SF / Wilful NSF / Large SF / Large NSF)

We do NOT dedup: each director/guarantor row is a distinct record so
all DIN/PAN values are preserved for downstream entity-resolution.
"""

import csv
import io
import os
import re
from datetime import datetime

import pandas as pd
from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://www.iob.bank.in/en/customers-care"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "iob_wilful_defaulters_134.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# Anchor-text -> source_list label
LABEL_MAP = [
    ("non suit filed wilful", "Non Suit Filed Wilful Defaulters"),
    ("suit filed wilful",     "Suit Filed Wilful Defaulters"),
    ("non suit filed large",  "Non Suit Filed Large Defaulters"),
    ("suit filed large",      "Suit Filed Large Defaulters"),
]


def _clean(v):
    if v is None or pd.isna(v):
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def _absolute(href):
    if href.startswith("http"):
        return href
    return "https://www.iob.bank.in" + href


def _discover_xlsx_links():
    r = Fetcher.get(LIST_URL, timeout=60, retries=1, retry_delay=0, verify=False)
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    found = {}
    pat = re.compile(
        r'''<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]{0,400}?)</a>''', re.I)
    for m in pat.finditer(body):
        href, raw_txt = m.group(1), m.group(2)
        txt = re.sub(r"<[^>]+>", " ", raw_txt)
        txt = re.sub(r"\s+", " ", txt).strip().lower()
        if "defaulter" not in txt:
            continue
        if not href.lower().split("?")[0].endswith(".xlsx") and "xlsx" not in href.lower():
            continue
        for kw, label in LABEL_MAP:
            if kw in txt and label not in found:
                found[label] = _absolute(href)
                break
    return found


def _row_to_record(row, source_list, doc_url, scraped_at):
    """Map one XLSX row (already a list of 17 values) to schema dict."""
    cycle, member_id, member_name, branch, state, b_name, b_pan, b_addr, \
        amt, suit_status, other_mem, d_name, din, d_pan, g_name, g_cin, g_pan \
        = (_clean(c) for c in (row + [""] * 17)[:17])

    if not b_name and not d_name and not g_name:
        return None

    name = b_name or d_name or g_name
    detail_parts = [f"Category: {source_list}"]
    if member_name:
        detail_parts.append(f"Member: {member_name}")
    if branch:
        detail_parts.append(f"Branch: {branch}")
    if state:
        detail_parts.append(f"State: {state}")
    if d_name:
        detail_parts.append(f"Director: {d_name}" + (f" (DIN {din})" if din else ""))
    if d_pan:
        detail_parts.append(f"Director PAN: {d_pan}")
    if g_name:
        detail_parts.append(f"Guarantor: {g_name}")
    if g_cin:
        detail_parts.append(f"Guarantor CIN: {g_cin}")
    if g_pan:
        detail_parts.append(f"Guarantor PAN: {g_pan}")
    if suit_status:
        detail_parts.append(f"Suit Status: {suit_status}")
    if cycle:
        detail_parts.append(f"Reporting Cycle: {cycle}")

    return {
        "source_agency": "IOB",
        "source_list": "Wilful & Large Defaulters",
        "case_unit": b_pan or din or g_pan,
        "name": name,
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": b_addr,
        "reward_amount": amt,
        "details": " | ".join(detail_parts),
        "has_document": "Yes",
        "document_url": doc_url,
        "detail_page_url": LIST_URL,
        "interpol_notice_id": "",
        "link_kind": "iob_defaulter",
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


def _scrape_xlsx(label, url, scraped_at):
    print(f"  fetching {label}: {url[:120]}")
    r = Fetcher.get(url, timeout=120, retries=1, retry_delay=0, verify=False)
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    if not body or len(body) < 200:
        print(f"    empty body for {label}")
        return []
    try:
        sheets = pd.read_excel(io.BytesIO(body), sheet_name=None, header=None)
    except Exception as e:
        print(f"    read_excel failed for {label}: {e}")
        return []
    out = []
    for sn, df in sheets.items():
        if df.shape[0] < 2:
            continue
        # Row 0 is header; data starts at row 1.
        for _, row in df.iloc[1:].iterrows():
            rec = _row_to_record(list(row), label, url, scraped_at)
            if rec:
                out.append(rec)
        print(f"    sheet={sn} rows={df.shape[0]} (data rows kept={sum(1 for _ in df.iloc[1:].iterrows())})")
    return out


def scrape():
    links = _discover_xlsx_links()
    if not links:
        raise RuntimeError("IOB: no defaulter XLSX links found on /en/customers-care")
    print(f"  discovered {len(links)} XLSX list(s):")
    for k, v in links.items():
        print(f"    - {k}: {v[:90]}...")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for label, url in links.items():
        out.extend(_scrape_xlsx(label, url, scraped_at))
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
    print("IOB Wilful & Large Defaulters scraper (#134)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("IOB: zero rows extracted")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
