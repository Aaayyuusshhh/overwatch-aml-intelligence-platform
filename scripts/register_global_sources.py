"""Register OpenSanctions + FATF sources in sources.json."""
import json
import os

NEW = [
    {"id": "opensanctions_debarment", "agency": "OpenSanctions",
     "list_name": "Debarment (Development Banks)",
     "url": "https://data.opensanctions.org/datasets/latest/debarment/targets.simple.csv",
     "status": "active", "region": "global", "list_type": "debarment",
     "notes": "World Bank + ADB + IDB + AfDB + EBRD debarred firms/individuals. Updated daily."},
    {"id": "opensanctions_crime", "agency": "OpenSanctions",
     "list_name": "Crime (Criminal Interest)",
     "url": "https://data.opensanctions.org/datasets/latest/crime/targets.simple.csv",
     "status": "active", "region": "global", "list_type": "criminal",
     "notes": "Interpol + wanted persons + criminal interest. Updated daily."},
    {"id": "opensanctions_peps", "agency": "OpenSanctions",
     "list_name": "Politically Exposed Persons (PEPs)",
     "url": "https://data.opensanctions.org/datasets/latest/peps/targets.simple.csv",
     "status": "active", "region": "global", "list_type": "pep",
     "notes": "PEPs from 28+ countries. Updated daily."},
    {"id": "fatf_blacklist", "agency": "Financial Action Task Force (FATF)",
     "list_name": "High-Risk Jurisdictions (Black List)",
     "url": "https://www.fatf-gafi.org/en/countries/black-and-grey-lists.html",
     "status": "active", "region": "global", "list_type": "jurisdiction_risk",
     "notes": "DPRK, Iran, Myanmar. Updated 3x/year."},
    {"id": "fatf_greylist", "agency": "Financial Action Task Force (FATF)",
     "list_name": "Jurisdictions under Increased Monitoring (Grey List)",
     "url": "https://www.fatf-gafi.org/en/countries/black-and-grey-lists.html",
     "status": "active", "region": "global", "list_type": "jurisdiction_risk",
     "notes": "22 jurisdictions as of Feb 2026 plenary. Updated 3x/year."},
]

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open('sources.json') as f:
        data = json.load(f)

    existing_ids = {s['id'] for s in data['sources']}

    added = []
    for src in NEW:
        if src['id'] not in existing_ids:
            data['sources'].append(src)
            existing_ids.add(src['id'])
            added.append(src['id'])
        else:
            print(f"  EXISTS: {src['id']}")

    with open('sources.json', 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Added {len(added)}: {added}")
    print(f"sources.json total: {len(data['sources'])}")
