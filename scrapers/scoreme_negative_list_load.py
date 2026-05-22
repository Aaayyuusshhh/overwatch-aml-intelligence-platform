"""
ScoreMe Negative List bulk dump loader.

Input:  data/sm_legal_data.negativeList.json (282MB, 96,417 records, 49 sources)
Output: one CSV per source under data/sm_*.csv (39 active sources)

Mapping rules:
  - entityName            -> name
  - fatherDirectorName    -> father_name
  - dateOfIncorporationBirth -> date_of_birth
  - gender                -> gender
  - address               -> address
  - orderCopy             -> document_url + has_document='Yes' (preferred)
  - link                  -> detail_page_url (or document_url if orderCopy empty
                              and link looks like a document URL)
  - All other non-empty fields are folded into the details string as
      "key: value | key: value | ..."
  - Skip meta fields: _id, source, _class, created_date, updated_date,
      updated_date_format, isCompleted.

Skip sources (overlap with existing DB data):
  - JSON-prompt's 6 (BSE Arbitration, OFAC, OFSI UK, SEBI Enforcement,
    EU Sanctions, World Bank Listing)
  - 4 detected collisions (NHB Penalties, NFRA Debarment, MSE Expelled,
    MSE Defaulter — all already loaded under existing source_ids)
"""
import csv, json, os, re
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJECT_ROOT, "data")
LOG = os.path.join(PROJECT_ROOT, "logs", "scoreme_load_20260522.log")
INPUT = os.path.join(DATA, "sm_legal_data.negativeList.json")

HEADER = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url", "interpol_notice_id",
          "link_kind", "scraped_at", "enrichment_status"]
NOW = datetime.now(timezone.utc).isoformat()

# Meta fields that never go into details
SKIP_DETAIL_FIELDS = {
    "_id", "source", "_class",
    "created_date", "updated_date", "updated_date_format", "isCompleted",
    "entityName", "fatherDirectorName", "dateOfIncorporationBirth",
    "gender", "address", "orderCopy", "link",
}

# JSON source name -> (source_id, agency, list_name, csv_filename)
SOURCE_MAP = {
    "Negative List NSDL":                                          ("sm_nsdl_negative",          "NSDL", "Negative List",                                    "sm_nsdl_negative.csv"),
    "Negative List BSE Delisted Companies":                        ("sm_bse_delisted",           "BSE", "Delisted Companies",                                "sm_bse_delisted.csv"),
    "Negative List DVAT Return Defaulters":                        ("sm_dvat_defaulters",        "Delhi VAT (DVAT)", "Return Defaulters",                    "sm_dvat_defaulters.csv"),
    "Negative List BSE Suspended":                                 ("sm_bse_suspended",          "BSE", "Suspended Companies",                               "sm_bse_suspended.csv"),
    "Negative List RERA Madhya Pradesh Registration Lapsed":       ("sm_rera_mp_lapsed",         "RERA Madhya Pradesh", "Registration Lapsed",               "sm_rera_mp_lapsed.csv"),
    "Negative List RERA Madhya Pradesh Defaulter Rejected Application": ("sm_rera_mp_rejected",  "RERA Madhya Pradesh", "Defaulter Rejected Application",    "sm_rera_mp_rejected.csv"),
    "Negative List RBI Imposed Penalty":                           ("sm_rbi_penalties",          "Reserve Bank of India (RBI)", "Imposed Penalties",         "sm_rbi_penalties.csv"),
    "Negative List Rajasthan Rera Lapsed":                         ("sm_rera_rajasthan_lapsed",  "RERA Rajasthan", "Registration Lapsed",                    "sm_rera_rajasthan_lapsed.csv"),
    "Negative List PEP Lok Sabha Members":                         ("sm_pep_lok_sabha",          "Parliament of India", "PEP Lok Sabha Members",             "sm_pep_lok_sabha.csv"),
    "Negative List IRDA warnings and penalties List":              ("sm_irda_warnings",          "IRDAI", "Warnings and Penalties",                          "sm_irda_warnings.csv"),
    "Negative List PEP Rajya Sabha Members":                       ("sm_pep_rajya_sabha",        "Parliament of India", "PEP Rajya Sabha Members",           "sm_pep_rajya_sabha.csv"),
    "Negative List Financial Intelligence Unit By Order":          ("sm_fiu_orders",             "Financial Intelligence Unit (FIU-IND)", "Orders",          "sm_fiu_orders.csv"),
    "Negative List East Zone Defaulter":                           ("sm_east_zone_defaulter",    "CBIC East Zone", "Defaulters",                              "sm_east_zone_defaulter.csv"),
    "Negative List RERA Madhya Pradesh Defaulter Unused Application": ("sm_rera_mp_unused",      "RERA Madhya Pradesh", "Defaulter Unused Application",      "sm_rera_mp_unused.csv"),
    "Negative List Money Control":                                 ("sm_money_control",          "MoneyControl", "Negative List",                            "sm_money_control.csv"),
    "Negative List NSE Expelled Members":                          ("sm_nse_expelled",           "NSE", "Expelled Members",                                  "sm_nse_expelled.csv"),
    "Negative List BSE Defaulter Expelled":                        ("sm_bse_defaulter_expelled", "BSE", "Defaulter / Expelled Members",                      "sm_bse_defaulter_expelled.csv"),
    "Negative List MCX Defaulters":                                ("sm_mcx_defaulters",         "Multi Commodity Exchange (MCX)", "Defaulters",             "sm_mcx_defaulters.csv"),
    "Negative List Income Tax Defaulters":                         ("sm_income_tax_defaulters",  "Income Tax Department", "Defaulters",                      "sm_income_tax_defaulters.csv"),
    "Negative List NSE Defaulter Members":                         ("sm_nse_defaulter",          "NSE", "Defaulter Members",                                 "sm_nse_defaulter.csv"),
    "Negative List Blacklisted NGOs":                              ("sm_blacklisted_ngos",       "Ministry of Home Affairs (MHA)", "Blacklisted NGOs",       "sm_blacklisted_ngos.csv"),
    "Negative List Rajasthan Rera Revoked":                        ("sm_rera_rajasthan_revoked", "RERA Rajasthan", "Registration Revoked",                   "sm_rera_rajasthan_revoked.csv"),
    "Negative List Blacklisted Doctors":                           ("sm_blacklisted_doctors",    "Medical Council of India (MCI)", "Blacklisted Doctors",    "sm_blacklisted_doctors.csv"),
    "Negative List MHA Terrorists":                                ("sm_mha_terrorists",         "Ministry of Home Affairs (MHA)", "Designated Terrorists",  "sm_mha_terrorists.csv"),
    "Negative List MutualFunds ARN Suspended":                     ("sm_amfi_arn_suspended",     "AMFI", "ARN Suspended",                                    "sm_amfi_arn_suspended.csv"),
    "Negative List RERA Uttar Pradesh Deregistered Projects":      ("sm_rera_up_deregistered",   "RERA Uttar Pradesh", "Deregistered Projects",              "sm_rera_up_deregistered.csv"),
    "Negative List MutualFunds ARN Terminated":                    ("sm_amfi_arn_terminated",    "AMFI", "ARN Terminated",                                   "sm_amfi_arn_terminated.csv"),
    "Negative List NSE Other Order Of MCSGFC":                     ("sm_nse_mcsgfc_orders",      "NSE", "Orders of MCSGFC",                                  "sm_nse_mcsgfc_orders.csv"),
    "Negative List NCDEX Defaulter Member":                        ("sm_ncdex_defaulter",        "NCDEX", "Defaulter Members",                               "sm_ncdex_defaulter.csv"),
    "Negative List Delhi RERA":                                    ("sm_rera_delhi",             "RERA Delhi", "Negative List",                              "sm_rera_delhi.csv"),
    "Negative List CBIC Service Tax Penalty Or Interest List":     ("sm_cbic_service_tax",       "CBIC", "Service Tax Penalty / Interest",                  "sm_cbic_service_tax.csv"),
    "Negative List NCDEX Expelled Member":                         ("sm_ncdex_expelled",         "NCDEX", "Expelled Members",                                "sm_ncdex_expelled.csv"),
    "Negative List CBIC Customs Penalty Or Interest List":         ("sm_cbic_customs_penalty",   "CBIC", "Customs Penalty / Interest",                      "sm_cbic_customs_penalty.csv"),
    "Negative List RERA Madhya Pradesh Registration Cancelled":    ("sm_rera_mp_cancelled",      "RERA Madhya Pradesh", "Registration Cancelled",            "sm_rera_mp_cancelled.csv"),
    "Negative List Rajasthan Rera Suspended":                      ("sm_rera_rajasthan_suspended","RERA Rajasthan", "Registration Suspended",                "sm_rera_rajasthan_suspended.csv"),
    "Negative List CBIC Customs Fraud And Collusion List":         ("sm_cbic_customs_fraud",     "CBIC", "Customs Fraud and Collusion",                     "sm_cbic_customs_fraud.csv"),
    "Negative List NCDEX Cessation Member":                        ("sm_ncdex_cessation",        "NCDEX", "Cessation Members",                              "sm_ncdex_cessation.csv"),
    "Negative List RERA Himachal Pradesh":                         ("sm_rera_hp",                "RERA Himachal Pradesh", "Negative List",                  "sm_rera_hp.csv"),
    "Negative List RBI NBFC":                                      ("sm_rbi_nbfc",               "Reserve Bank of India (RBI)", "NBFC Negative List",        "sm_rbi_nbfc.csv"),
}

# JSON source names whose data overlaps with existing DB sources — skip entirely.
SKIP_SOURCES = {
    # Per prompt:
    "Negative List BSE Arbitration Award",
    "Negative List OFAC",
    "Negative List OFSI UK Sanction List",
    "Negative List SEBI Enforcement",
    "Negative List EU Sanctions",
    "Negative List World Bank Listing of Ineligible Firms and Individuals List",
    # 4 collisions detected from sources.json (existing IDs already loaded):
    "Negative List National Housing Bank Penalties",
    "Negative List NFRA Debarment",
    "Negative List MSE Expelled Members",
    "Negative List MSE Defaulter Members",
}

DOC_EXT_RE = re.compile(r"\.(pdf|xlsx?|csv|docx?|html?)(?:\?|$)", re.I)


def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def build_details(rec):
    parts = []
    for k, v in rec.items():
        if k in SKIP_DETAIL_FIELDS:
            continue
        if v is None:
            continue
        s = str(v).strip()
        if not s or s == "{}" or s == "[]":
            continue
        parts.append(f"{k}: {s}")
    return " | ".join(parts)


def map_record(rec, agency, list_name):
    name = (rec.get("entityName") or "").strip()
    if not name:
        return None
    order_copy = (rec.get("orderCopy") or "").strip()
    link       = (rec.get("link") or "").strip()
    document_url = order_copy
    detail_url   = link
    has_doc = "Yes" if document_url else ""
    # If only `link` is set and it points to a document file, promote it to document_url
    if not document_url and link and DOC_EXT_RE.search(link):
        document_url = link
        has_doc = "Yes"
    row = {h: "" for h in HEADER}
    row["source_agency"]   = agency
    row["source_list"]     = list_name
    row["name"]            = name[:500]
    row["father_name"]     = (rec.get("fatherDirectorName") or "").strip()
    row["date_of_birth"]   = (rec.get("dateOfIncorporationBirth") or "").strip()
    row["gender"]          = (rec.get("gender") or "").strip()
    row["address"]         = (rec.get("address") or "").strip()
    row["details"]         = build_details(rec)
    row["has_document"]    = has_doc
    row["document_url"]    = document_url
    row["detail_page_url"] = detail_url
    row["scraped_at"]      = NOW
    return row


def main():
    log(f"reading {INPUT} ...")
    with open(INPUT) as f:
        data = json.load(f)
    log(f"  {len(data):,} records loaded")

    # Bucket records by source
    buckets = {}    # source_name -> list of rows
    skip_counts = {}
    unmapped = {}   # source_name -> count (in JSON but not in SOURCE_MAP and not in SKIP)
    for rec in data:
        src_name = rec.get("source", "")
        if src_name in SKIP_SOURCES:
            skip_counts[src_name] = skip_counts.get(src_name, 0) + 1
            continue
        if src_name not in SOURCE_MAP:
            unmapped[src_name] = unmapped.get(src_name, 0) + 1
            continue
        sid, agency, list_name, _ = SOURCE_MAP[src_name]
        row = map_record(rec, agency, list_name)
        if row is None:
            continue
        buckets.setdefault(src_name, []).append(row)

    # Write CSVs
    total_written = 0
    for src_name, (_, _, _, fname) in SOURCE_MAP.items():
        rows = buckets.get(src_name, [])
        path = os.path.join(DATA, fname)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
        total_written += len(rows)
        log(f"  wrote {len(rows):>6d} rows -> {fname}")

    log(f"\nTOTAL written: {total_written:,} rows across {len(SOURCE_MAP)} CSVs")
    log(f"SKIPPED (overlap):")
    for s, n in sorted(skip_counts.items(), key=lambda x: -x[1]):
        log(f"  {n:>5d}  {s}")
    if unmapped:
        log(f"\nUNMAPPED JSON sources (will not be loaded):")
        for s, n in sorted(unmapped.items(), key=lambda x: -x[1]):
            log(f"  {n:>5d}  {s}")


if __name__ == "__main__":
    main()
