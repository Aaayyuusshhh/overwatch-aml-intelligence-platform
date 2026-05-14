"""
CCI Antitrust Orders (#17).

Source: https://cci.gov.in/antitrust-orders

The visible page is a DataTables widget with an empty <tbody>; the
table content is filled from a JSON endpoint at
  https://cci.gov.in/antitrust/orders/list
which returns the standard DataTables payload
{draw, recordsTotal, recordsFiltered, data:[...]}.

Each item has:
  id, case_no, type, description, antitrust_categories_id,
  order_date, main_order_date, file_content (JSON-string of attached
  PDF files), title (a coloured Section tag), files (rendered HTML
  with PDF link).

We extract the parties from `description`. CCI cases are titled
"<Informant> Vs. <Opposite Party>" — the opposite party is what
matters for screening, so the cleaner splits at "Vs." (case-insensitive,
allowing variants like "vs", "V/s", "v.") and keeps the right side as
the canonical name. If no Vs. delimiter is found we keep the full
description as the name.
"""

import csv
import html
import json
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_URL = "https://cci.gov.in/antitrust/orders/list"
DETAIL_BASE = "https://cci.gov.in/antitrust/orders/details/"
DOC_BASE = "https://cci.gov.in/"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "cci_antitrust_orders_17.csv")

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
      "Accept": "application/json",
      "X-Requested-With": "XMLHttpRequest"}

VS_RE = re.compile(r"\s+(?:[vV]\.?[sS]\.?|[vV]\.?)\s+|\s+[vV]/[sS]\s+", re.U)


def _clean(s):
    if s is None:
        return ""
    s = html.unescape(str(s))
    s = re.sub(r"<[^>]+>", " ", s)         # strip HTML
    s = s.replace("\xa0", " ").replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip(" .,;-")
    return s


def _extract_party(description):
    """Return (opposite_party, informant). When there's no Vs.
    delimiter we treat the whole description as the opposite party."""
    d = _clean(description)
    if not d:
        return "", ""
    m = VS_RE.search(d)
    if not m:
        return d, ""
    left, right = d[:m.start()].strip(), d[m.end():].strip()
    return right or d, left


def _first_pdf(item):
    """Return the first attached PDF URL, if any."""
    raw = item.get("file_content") or ""
    raw = html.unescape(raw)
    try:
        files = json.loads(raw)
    except Exception:
        files = []
    for f in files or []:
        fn = (f.get("file_name") or "").strip()
        if fn:
            return urljoin(DOC_BASE, fn)
    # Fallback: parse the rendered 'files' HTML for an <a href=>.
    m = re.search(r'href="([^"]+\.pdf[^"]*)"', item.get("files") or "", re.I)
    if m:
        return m.group(1)
    return ""


def scrape():
    r = requests.get(API_URL, headers=UA, timeout=60, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"CCI: API returned {r.status_code}")
    payload = r.json()
    items = payload.get("data") or []
    print(f"  fetched {len(items)} antitrust orders "
          f"(recordsTotal={payload.get('recordsTotal')})")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for it in items:
        case_no = _clean(it.get("case_no", ""))
        opposite, informant = _extract_party(it.get("description", ""))
        if not opposite:
            continue
        section = _clean(it.get("title", ""))
        type_ = _clean(it.get("type", ""))
        order_date = _clean(it.get("order_date", ""))
        main_date = _clean(it.get("main_order_date", ""))
        doc_url = _first_pdf(it)
        details = " | ".join(p for p in [
            f"Case No: {case_no}" if case_no else "",
            f"Section: {section}" if section else "",
            f"Type: {type_}" if type_ else "",
            f"Order Date: {order_date}" if order_date else "",
            (f"Main Order Date: {main_date}"
                if main_date and main_date != order_date else ""),
            f"Informant: {informant}" if informant else "",
        ] if p)
        detail_page = urljoin(DETAIL_BASE, f"{it.get('id')}/0")
        out.append({
            "source_agency": "Competition Commission of India (CCI)",
            "source_list": "Antitrust Orders",
            "case_unit": case_no,
            "name": opposite,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": "",
            "reward_amount": "",
            "details": details,
            "has_document": "Yes" if doc_url else "No",
            "document_url": doc_url,
            "detail_page_url": detail_page,
            "interpol_notice_id": "",
            "link_kind": "constructed",
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
    print("CCI Antitrust Orders (#17)")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
