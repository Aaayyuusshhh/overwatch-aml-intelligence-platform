"""
CDSL Defaulting Clients (#162) — BSE + NSE variants.

Both files are pre-staged in data/:
  data/BSE List of Defaulting_Clients.xlsx           (92 rows)
  data/NSE Defaulting_Client_Database 2.xlsx         (719 rows)

The BSE file has a one-row title above the header — pandas reads
the title as the column row, so we skip the first non-data row.
The NSE file has the header as the first row directly.

Both share an essentially equivalent schema after the header row:
  Sr No | (BSE Notice #) | (Date) | Name of Defaulting Client |
  PAN | Trading Member | Arbitration/Appeal Ref | Date of Award |
  Quantum of Default | Exchange

Output CSV (in 17-col schema):
  name      = Name of Defaulting Client
  case_unit = PAN of Client
  details   = Trading Member: … | Award Ref: … | Date: … | Amount: … | Exchange: …
"""

import csv
import os
import re
from datetime import datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BSE_XLSX  = os.path.join(PROJECT_ROOT, "data", "BSE List of Defaulting_Clients.xlsx")
NSE_XLSX  = os.path.join(PROJECT_ROOT, "data", "NSE Defaulting_Client_Database 2.xlsx")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "cdsl_defaulting_clients_162.csv")

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
    s = str(s)
    if s.lower() in ("nan", "nat", "none"):
        return ""
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip(" .,;-")


def _norm_date(v):
    """pandas may return Timestamps or strings; normalise to ISO date
    or pass through the string."""
    if v is None or pd.isna(v):
        return ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    return _clean(v)


# ----- BSE ----------------------------------------------------------------
def _scrape_bse(scraped_at):
    # Row 0 is a title; row 1 is the actual header. Use header=1.
    df = pd.read_excel(BSE_XLSX, header=1)
    df.columns = [str(c).strip() for c in df.columns]
    out = []
    for _, r in df.iterrows():
        name = _clean(r.get("Name of  Defaulting Client",
                            r.get("Name of Defaulting Client", "")))
        if not name or name.lower() in {"name of defaulting client",
                                          "name of  defaulting client"}:
            continue
        pan  = _clean(r.get("Pan of Client", ""))
        tm   = _clean(r.get("Name of trading member", ""))
        notice_no   = _clean(r.get("BSE Notice  Number",
                                    r.get("BSE Notice Number", "")))
        notice_date = _norm_date(r.get("BSE Notice\ndate",
                                        r.get("BSE Notice date", "")))
        award_ref   = _clean(r.get("Arbitration/Appeal Award ref no.", ""))
        award_date  = _norm_date(r.get("Date of Arbitration/Appeal  Award",
                                        r.get("Date of Arbitration/Appeal Award", "")))
        qty         = _clean(r.get("Quantum of Default", ""))
        exch        = _clean(r.get("Exchange", ""))
        details_bits = []
        if tm:          details_bits.append(f"Trading Member: {tm}")
        if award_ref:   details_bits.append(f"Award Ref: {award_ref}")
        if award_date:  details_bits.append(f"Award Date: {award_date}")
        if notice_no:   details_bits.append(f"BSE Notice: {notice_no}")
        if notice_date: details_bits.append(f"Notice Date: {notice_date}")
        if qty:         details_bits.append(f"Quantum: {qty}")
        if exch:        details_bits.append(f"Exchange: {exch}")
        out.append({
            "source_agency": "Central Depository Services (India) Limited (CDSL)",
            "source_list":   "Defaulting Clients",
            "case_unit":     pan,
            "name":          name,
            "father_name":   "",
            "date_of_birth": "",
            "gender":        "",
            "address":       "",
            "reward_amount": qty,
            "details":       " | ".join(details_bits),
            "has_document":  "No",
            "document_url":  "",
            "detail_page_url": "",
            "interpol_notice_id": "",
            "link_kind":     "manual_discovery",
            "scraped_at":    scraped_at,
            "enrichment_status": "",
        })
    return out


# ----- NSE ----------------------------------------------------------------
def _scrape_nse(scraped_at):
    df = pd.read_excel(NSE_XLSX)
    df.columns = [str(c).strip() for c in df.columns]
    out = []
    for _, r in df.iterrows():
        name = _clean(r.get("Name of the Defaulting client", ""))
        if not name or name.lower() == "name of the defaulting client":
            continue
        pan = _clean(r.get("Pan of Client", ""))
        tm  = _clean(r.get("Name of the trading member", ""))
        case_ref = _clean(r.get(
            "Complaint No. / Arbitration / Appellate Matter No.", ""))
        order_date = _norm_date(r.get("Date of Order / Award", ""))
        award_dets = _clean(r.get("Award details", ""))
        exch       = _clean(r.get("Exchange", ""))
        details_bits = []
        if tm:        details_bits.append(f"Trading Member: {tm}")
        if case_ref:  details_bits.append(f"Case Ref: {case_ref}")
        if order_date:details_bits.append(f"Order Date: {order_date}")
        if award_dets:details_bits.append(f"Award: {award_dets}")
        if exch:      details_bits.append(f"Exchange: {exch}")
        out.append({
            "source_agency": "Central Depository Services (India) Limited (CDSL)",
            "source_list":   "Defaulting Clients",
            "case_unit":     pan,
            "name":          name,
            "father_name":   "",
            "date_of_birth": "",
            "gender":        "",
            "address":       "",
            "reward_amount": "",
            "details":       " | ".join(details_bits),
            "has_document":  "No",
            "document_url":  "",
            "detail_page_url": "",
            "interpol_notice_id": "",
            "link_kind":     "manual_discovery",
            "scraped_at":    scraped_at,
            "enrichment_status": "",
        })
    return out


def _save(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {path}")


def run():
    print("=" * 60)
    print("CDSL Defaulting Clients (#162) — BSE + NSE")
    print("=" * 60)
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bse = _scrape_bse(scraped_at)
    nse = _scrape_nse(scraped_at)
    rows = bse + nse
    _save(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
