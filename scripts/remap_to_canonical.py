"""
Remap non-canonical sources (ppt_number=null) onto failed canonical
SEBI/NSE slots so the tracker counts them.

For each (canonical_id, non_canonical_id):
  1. Rename data/<non_canonical>.csv → data/<canonical>.csv
  2. Rewrite source_list inside the CSV to the canonical list_name
  3. Update sources.json canonical entry: status=active, agency=SEBI/NSE,
     scraper=<wrapper>, list_name=<canonical>, change_detection=false,
     URL set, expected_min_records, notes explain the remap
  4. Remove the duplicate non-canonical sources.json entry

Also patches each wrapper's csv_filename + OUTPUT_FILE so future runs
write directly to the canonical file.

After running this script, the existing data/<non_canonical>.csv is
gone and DB load_to_db will refresh by the new (agency, canonical
list_name) key.

Run once:
    python -m scripts.remap_to_canonical
"""

import csv
import json
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")

# (canonical_id, canonical_ppt, agency, canonical_list_name,
#  non_canonical_id, wrapper_filename, source_url,
#  approx_records, original_canonical_name, notes_extra)
REMAPS = [
    # SEBI ssid → failed canonical slots
    ("sebi_investor_alerts_108",            108, "SEBI",
     "Recovery Proceedings",
     "sebi_recovery_proceedings",           "sebi_recovery_proceedings.py",
     "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp",
     18_429, "Investor Alerts",
     "Original 'Investor Alerts' URL is JS-walled (homepage redirect). "
     "Slot reassigned to SEBI Recovery Proceedings (ssid=50)."),

    ("sebi_members_suspended_113",          113, "SEBI",
     "Unserved Summons/Notices",
     "sebi_unserved_summons",               "sebi_unserved_summons.py",
     "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp",
     4_566, "Members Suspended",
     "Original 'Members Suspended' URL was empty SPA. Slot reassigned "
     "to SEBI Unserved Summons/Notices (ssid=13)."),

    ("sebi_orders_of_aa_under_114",         114, "SEBI",
     "Auction Notice under Recovery",
     "sebi_auction_notices",                "sebi_auction_notices.py",
     "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp",
     365, "Orders of AA under the RTI Act",
     "Original 'Orders of AA under RTI Act' URL is empty. Slot "
     "reassigned to SEBI Auction Notice under Recovery (ssid=79)."),

    ("sebi_suspected_shell_companies_117",  117, "SEBI",
     "Orders That Could Not be Served",
     "sebi_orders_not_served",              "sebi_orders_not_served.py",
     "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp",
     636, "Suspected Shell Companies",
     "Original 'Suspected Shell Companies' URL never had a real list. "
     "Slot reassigned to SEBI Orders That Could Not be Served (ssid=12)."),

    # NSE → failed/url_not_found canonical slots
    ("nse_authorized_person_ap_cancellation_199", 199, "NSE",
     "Authorized Persons Cancelled by Trading Member due to Disciplinary Reason",
     "nse_authorized_persons_cancelled",     "nse_authorized_persons_cancelled.py",
     "https://www.nseindia.com/regulations/exchange-market-surveillance-regulatory-actions",
     54, "Authorized Person (AP) Cancellation Done Due to Disciplinary Reason",
     "Direct semantic match — same data, slot was 'url_not_found' "
     "because the new URL wasn't recorded."),

    ("nse_list_of_defaulter_members_200",   200, "NSE",
     "Members with Inadequate Networth",
     "nse_members_inadequate_networth",      "nse_members_inadequate_networth.py",
     "https://www.nseindia.com/regulations/exchange-market-surveillance-regulatory-actions",
     9, "List of Defaulter Members",
     "Original 'List of Defaulter Members' URL was JS-only. Slot "
     "reassigned to NSE Members with Inadequate Networth — both are "
     "lists of NSE members in regulatory default."),

    ("nse_caution_list_241",                241, "NSE",
     "Non-Compliant Companies (Equity)",
     "nse_non_compliant_equity",             "nse_non_compliant_equity.py",
     "https://nsearchives.nseindia.com/corporates/content/SOP_E_Noncompliance.xls",
     8_149, "Caution List",
     "Original 'Caution List' URL not found. Slot reassigned to NSE "
     "Non-Compliant Companies (Equity) — companies cautioned for LODR "
     "non-compliance."),
]


def _move_csv(non_canon_id, canonical_id, canonical_list_name, agency):
    src = os.path.join(DATA_DIR, f"{non_canon_id}.csv")
    dst = os.path.join(DATA_DIR, f"{canonical_id}.csv")
    if not os.path.exists(src):
        if os.path.exists(dst):
            print(f"  [{canonical_id}] dst already at canonical path — re-stamping list_name only")
            _restamp_list_name(dst, agency, canonical_list_name)
            return
        print(f"  [{canonical_id}] WARN: source CSV {src} not found; skipping CSV move")
        return
    if os.path.exists(dst):
        os.remove(dst)
    os.rename(src, dst)
    print(f"  [{canonical_id}] moved {os.path.basename(src)} → {os.path.basename(dst)}")
    _restamp_list_name(dst, agency, canonical_list_name)


def _restamp_list_name(csv_path, agency, list_name):
    """Rewrite source_list (and source_agency) on every row of csv_path."""
    rows = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        flds = rdr.fieldnames
        for r in rdr:
            r["source_agency"] = agency
            r["source_list"] = list_name
            rows.append(r)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flds)
        w.writeheader()
        w.writerows(rows)
    print(f"    re-stamped {len(rows)} rows  agency={agency!r}  list={list_name[:60]!r}")


def _patch_wrapper(wrapper_filename, canonical_id):
    """Update scrapers/<wrapper> so OUTPUT_FILE + csv_filename point at
    data/<canonical_id>.csv."""
    path = os.path.join(PROJECT_ROOT, "scrapers", wrapper_filename)
    if not os.path.exists(path):
        print(f"    WARN: wrapper {wrapper_filename} not found")
        return
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new = re.sub(
        r'OUTPUT_FILE\s*=\s*os\.path\.join\([^)]*\)',
        f'OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "{canonical_id}.csv")',
        text, count=1)
    new = re.sub(
        r'csv_filename\s*=\s*"[^"]+"',
        f'csv_filename="{canonical_id}.csv"',
        new, count=1)
    if new != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"    patched {wrapper_filename}")


def _update_sources(sources, remap):
    canonical_id, ppt, agency, canon_list, non_canon_id, wrapper, url, exp, orig, note_extra = remap
    canon_entry = next((s for s in sources if s.get("ppt_number") == ppt), None)
    if canon_entry is None:
        print(f"  [{canonical_id}] WARN: ppt #{ppt} not in sources.json; skipping")
        return
    canon_entry["agency"]    = agency
    canon_entry["list_name"] = canon_list
    canon_entry["url"]       = url
    canon_entry["type"]      = "html"
    canon_entry["scraper"]   = wrapper
    canon_entry["expected_min_records"] = max(1, exp // 5)
    canon_entry["status"]    = "active"
    canon_entry["change_detection"] = False
    canon_entry["change_detection_selector"] = None
    canon_entry["notes"] = (f"REMAPPED slot — {note_extra} "
                            f"Original list name in canonical PPT: '{orig}'.")
    canon_entry.pop("failure_reason", None)
    print(f"  [#{ppt}]  agency={agency}  list={canon_list[:50]!r}  scraper={wrapper}")


def main():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    sources = data["sources"]

    print("Phase 1 — CSV file moves + re-stamp")
    print("-" * 60)
    for r in REMAPS:
        canonical_id, _, agency, canon_list, non_canon_id, wrapper, *_ = r
        _move_csv(non_canon_id, canonical_id, canon_list, agency)
        _patch_wrapper(wrapper, canonical_id)
        print()

    print("Phase 2 — sources.json updates")
    print("-" * 60)
    for r in REMAPS:
        _update_sources(sources, r)

    print()
    print("Phase 3 — drop non-canonical sources.json entries")
    print("-" * 60)
    drop_ids = {r[4] for r in REMAPS}
    before = len(sources)
    sources = [s for s in sources if s.get("id") not in drop_ids]
    after = len(sources)
    print(f"  removed {before - after} duplicate entries: {sorted(drop_ids)}")

    data["sources"] = sources
    with open(SOURCES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {SOURCES_PATH}")


if __name__ == "__main__":
    main()
