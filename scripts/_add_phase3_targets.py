#!/usr/bin/env python3
"""Register Phase-3 India PSU + state-police + RERA + regulator targets.

These get added with status=blocked so scrape_blocked_sources.py picks them
up on its next pass. We attach a specific list-page URL where I could find
one; otherwise the agency landing page (still a valid proof link)."""
import json
import os

SOURCES_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources.json")


def src(sid, agency, list_name, url, country="IN", notes=""):
    return {
        "id": sid, "agency": agency, "list_name": list_name, "url": url,
        "type": "html", "scraper": None, "expected_min_records": 0,
        "status": "blocked", "change_detection": False,
        "change_detection_selector": None, "country": country,
        "notes": notes,
    }


TARGETS = [
    # India PSU debarred / blacklisted vendor pages — direct list URLs where known.
    src("indian_railways_debarred",
        "Indian Railways", "Debarred Contractors",
        "https://contracts.indianrailways.gov.in/",
        notes="IREPS contractor debarment register."),
    src("nhpc_debarred_vendors",
        "NHPC Limited", "Debarred Vendors",
        "https://www.nhpcindia.com/tenders/blacklisted-firms",
        notes="NHPC blacklisted firms list."),
    src("ntpc_blacklisted",
        "NTPC Limited", "Debarred Agencies",
        "https://www.ntpc.co.in/en/corporate-citizens/debarred-agencies",
        notes="NTPC debarred agencies list."),
    src("coal_india_banned",
        "Coal India Limited", "Banned/Debarred Firms",
        "https://www.coalindia.in/en-us/transparency/vendor-debarred.aspx",
        notes="Coal India debarred vendor list."),
    src("power_grid_debarred",
        "Power Grid Corporation of India", "Banned/Debarred Vendors",
        "https://www.powergrid.in/banneddebarred-vendor-list",
        notes="POWERGRID debarred vendor list."),
    src("sail_blacklisted",
        "Steel Authority of India (SAIL)", "Banned/Holiday Listed Vendors",
        "https://sail.co.in/",
        notes="SAIL banned/holiday-listed vendor register."),
    src("hal_debarred",
        "Hindustan Aeronautics Limited (HAL)", "Debarred Vendors",
        "https://hal-india.co.in/",
        notes="HAL debarred vendor list."),
    src("bharat_electronics_debarred",
        "Bharat Electronics Limited (BEL)", "Banned/Holiday-Listed Vendors",
        "https://bel-india.in/",
        notes="BEL banned vendor list."),
    src("drdo_debarred",
        "Defence Research and Development Organisation (DRDO)", "Banned/Debarred Vendors",
        "https://www.drdo.gov.in/",
        notes="DRDO banned vendor list."),
    src("aai_blacklisted",
        "Airports Authority of India (AAI)", "Banned/Debarred Contractors",
        "https://www.aai.aero/",
        notes="AAI banned contractor list."),
    src("nhai_debarred",
        "National Highways Authority of India (NHAI)", "Debarred Concessionaires/Contractors",
        "https://nhai.gov.in/",
        notes="NHAI debarred contractor list."),
    src("cpwd_debarred",
        "Central Public Works Department (CPWD)", "Banned/Debarred Contractors",
        "https://cpwd.gov.in/",
        notes="CPWD debarred contractor list."),

    # State police wanted (covers states not yet in DB)
    src("tn_police_wanted_eservices",
        "Tamil Nadu Police", "Most Wanted (eServices)",
        "https://eservices.tnpolice.gov.in/CCTNSNICSDC/",
        notes="TN Police CCTNS public portal — most-wanted criminals."),
    src("wb_cid_wanted_page",
        "West Bengal CID", "Wanted Persons",
        "https://cidwestbengal.gov.in/wanted",
        notes="West Bengal CID wanted persons page."),
    src("assam_police_wanted_page",
        "Assam Police", "Wanted Persons",
        "https://police.assam.gov.in/",
        notes="Assam Police wanted persons page."),
    src("hp_police_wanted_page",
        "Himachal Pradesh Police", "Wanted Persons",
        "https://hppolice.gov.in/",
        notes="HP Police wanted persons page."),
    src("cg_police_wanted_page",
        "Chhattisgarh Police", "Wanted Persons",
        "https://cgpolice.gov.in/",
        notes="Chhattisgarh Police wanted persons page."),

    # RBI various
    src("rbi_nbfc_list",
        "Reserve Bank of India (RBI)", "List of NBFCs",
        "https://www.rbi.org.in/Scripts/BS_NBFCList.aspx",
        notes="RBI list of NBFCs (proof reference for due diligence)."),
    src("rbi_cancelled_nbfc",
        "Reserve Bank of India (RBI)", "Cancelled NBFC Licenses",
        "https://rbi.org.in/Scripts/BS_NBFCCancel.aspx",
        notes="RBI cancelled NBFC registrations."),
    src("rbi_unauthorized_forex",
        "Reserve Bank of India (RBI)", "Alert List - Unauthorized Forex Dealers",
        "https://rbi.org.in/Scripts/BS_ViewAlertList.aspx",
        notes="RBI alert list of unauthorised forex dealers."),

    # SEBI / IRDAI / CAG
    src("sebi_debarred_latest",
        "Securities and Exchange Board of India (SEBI)", "Debarred Entities (Latest Orders)",
        "https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doRecognisedFpi=yes",
        notes="SEBI debarred entities (latest orders)."),
    src("irdai_liquidation",
        "IRDAI", "Insurance Companies Under Liquidation",
        "https://www.irdai.gov.in/",
        notes="IRDAI insurance companies under liquidation."),
    src("cag_audit_paras",
        "Comptroller and Auditor General of India (CAG)", "Audit Paras - Fraud / Embezzlement",
        "https://cag.gov.in/",
        notes="CAG audit paras flagging fraud / embezzlement."),

    # More RERA states with deeper paths
    src("rera_wb_hira_orders",
        "West Bengal HIRA", "Lapsed/Revoked Projects",
        "https://wbhira.gov.in/",
        notes="WB-HIRA penalty / revocation orders."),
    src("rera_bihar_orders",
        "Bihar RERA", "Penalty Orders",
        "https://rera.bihar.gov.in/",
        notes="Bihar RERA penalty orders."),
    src("rera_odisha_orders",
        "Odisha RERA", "Penalty Orders",
        "https://rera.odisha.gov.in/",
        notes="Odisha RERA penalty orders."),
    src("rera_punjab_orders",
        "Punjab RERA", "Penalty Orders",
        "https://rera.punjab.gov.in/",
        notes="Punjab RERA penalty orders."),
    src("rera_goa_orders",
        "Goa RERA", "Penalty Orders",
        "https://rera.goa.gov.in/",
        notes="Goa RERA penalty orders."),
]


def main():
    with open(SOURCES_JSON) as f:
        data = json.load(f)
    have = {s["id"] for s in data["sources"]}
    added = []
    for t in TARGETS:
        if t["id"] in have:
            continue
        data["sources"].append(t)
        have.add(t["id"])
        added.append(t["id"])
    with open(SOURCES_JSON, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Added {len(added)} Phase-3 targets")
    for sid in added:
        print(f"  + {sid}")
    print(f"\nTotal sources: {len(data['sources'])}")


if __name__ == "__main__":
    main()
