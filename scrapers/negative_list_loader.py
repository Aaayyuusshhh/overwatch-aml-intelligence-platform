"""
ScoreMe Negative List JSON dump loader.

Reads 4 MongoDB-export JSON files under data/, maps records to the canonical
17-column watchlist schema, and writes one CSV per (sub-)source under data/.

Output source_ids:
  cbic_customs_fraud, cbic_customs_penalty, cbic_service_tax_penalty,
  bse_arbitration_awards, mse_expelled_members, mse_defaulter_members
"""

import csv
import json
import os
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

HEADER = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# Skip these keys when building the details string.
SKIP_KEYS = {"_id", "entityName", "source", "_class"}

# (source_id, source_agency, source_list, source-field substring matcher)
ROUTES = [
    # CBIC: 3 sub-sources distinguished by record["source"] substrings.
    ("cbic_customs_fraud",
     "Central Board of Indirect Taxes (CBIC)",
     "Customs Fraud & Collusion List",
     lambda s: "fraud" in s.lower()),
    ("cbic_customs_penalty",
     "Central Board of Indirect Taxes (CBIC)",
     "Customs Penalty/Interest List",
     lambda s: "customs penalty" in s.lower()),
    ("cbic_service_tax_penalty",
     "Central Board of Indirect Taxes (CBIC)",
     "Service Tax Penalty/Interest",
     lambda s: "service tax" in s.lower()),
    # Single-source files.
    ("bse_arbitration_awards",
     "Bombay Stock Exchange (BSE)",
     "Arbitration Awards",
     lambda s: True),
    ("mse_expelled_members",
     "Metropolitan Stock Exchange (MSE)",
     "Expelled Members",
     lambda s: True),
    ("mse_defaulter_members",
     "Metropolitan Stock Exchange (MSE)",
     "Defaulter Members",
     lambda s: True),
]

JOBS = [
    ("CBIC_data_negativeList.json",
     ["cbic_customs_fraud", "cbic_customs_penalty", "cbic_service_tax_penalty"]),
    ("bse_data_negativeList.json", ["bse_arbitration_awards"]),
    ("MSE_Expelled_Members_data_negativeList.json", ["mse_expelled_members"]),
    ("MSE_Defaulter_Members_data_negativeList.json", ["mse_defaulter_members"]),
]

ROUTE_MAP = {r[0]: r for r in ROUTES}


def stringify(v):
    if v is None:
        return ""
    if isinstance(v, dict):
        # MongoDB extended JSON ({"$oid": "..."}, {"$date": "..."}) -> inner value.
        if len(v) == 1 and next(iter(v)).startswith("$"):
            return str(next(iter(v.values())))
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, list):
        return ", ".join(stringify(x) for x in v if x not in (None, "", [], {}))
    return str(v).strip()


def build_details(rec):
    parts = []
    for k, v in rec.items():
        if k in SKIP_KEYS:
            continue
        s = stringify(v)
        if not s:
            continue
        parts.append(f"{k}: {s}")
    return " | ".join(parts)


def route_for_cbic(src):
    for r in ROUTES[:3]:
        if r[3](src):
            return r
    return None


def main():
    now = datetime.now(timezone.utc).isoformat()
    rows_by_sid = {r[0]: [] for r in ROUTES}

    for fname, allowed_sids in JOBS:
        path = os.path.join(DATA_DIR, fname)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        print(f"[load] {fname}: {len(data)} records")

        if len(allowed_sids) == 1:
            sid = allowed_sids[0]
            route = ROUTE_MAP[sid]
            for rec in data:
                rows_by_sid[sid].append((route, rec))
        else:
            unmatched = 0
            for rec in data:
                route = route_for_cbic(rec.get("source", ""))
                if route is None:
                    unmatched += 1
                    continue
                rows_by_sid[route[0]].append((route, rec))
            if unmatched:
                print(f"  WARN: {unmatched} CBIC records with unmatched source")

    for sid, items in rows_by_sid.items():
        route = ROUTE_MAP[sid]
        out_path = os.path.join(DATA_DIR, f"{sid}.csv")
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADER)
            w.writeheader()
            for _, rec in items:
                w.writerow({
                    "source_agency": route[1],
                    "source_list": route[2],
                    "case_unit": "",
                    "name": stringify(rec.get("entityName", "")),
                    "father_name": stringify(rec.get("fatherDirectorName", "")),
                    "date_of_birth": stringify(rec.get("dateOfIncorporationBirth", "")),
                    "gender": stringify(rec.get("gender", "")),
                    "address": stringify(rec.get("address", "")),
                    "reward_amount": "",
                    "details": build_details(rec),
                    "has_document": "",
                    "document_url": "",
                    "detail_page_url": stringify(rec.get("link", "")),
                    "interpol_notice_id": "",
                    "link_kind": "",
                    "scraped_at": now,
                    "enrichment_status": "",
                })
        print(f"[write] {sid}.csv -> {len(items)} rows")


if __name__ == "__main__":
    main()
