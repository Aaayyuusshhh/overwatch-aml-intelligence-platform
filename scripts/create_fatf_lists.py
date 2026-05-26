"""Create FATF black + grey list CSV (verified Feb 2026 plenary)."""
import csv
import os
from datetime import datetime

FIELDS = ['source_agency', 'source_list', 'case_unit', 'name', 'father_name',
          'date_of_birth', 'gender', 'address', 'reward_amount', 'details',
          'has_document', 'document_url', 'detail_page_url', 'interpol_notice_id',
          'link_kind', 'scraped_at', 'enrichment_status']

URL = "https://www.fatf-gafi.org/en/countries/black-and-grey-lists.html"
now = datetime.utcnow().isoformat()[:19]

FATF_BLACK = ["Iran", "DPRK (North Korea)", "Myanmar"]
FATF_GREY = [
    "Algeria", "Angola", "Bolivia", "Bulgaria", "Cameroon",
    "Côte d'Ivoire", "Democratic Republic of the Congo", "Haiti",
    "Kenya", "Kuwait", "Lao PDR", "Lebanon", "Monaco",
    "Namibia", "Nepal", "Papua New Guinea", "South Sudan",
    "Syria", "Venezuela", "Vietnam", "Virgin Islands (UK)", "Yemen",
]

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open('data/fatf_lists.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        blank = {k: '' for k in FIELDS}
        for country in FATF_BLACK:
            w.writerow({**blank,
                'source_agency': 'Financial Action Task Force (FATF)',
                'source_list': 'High-Risk Jurisdictions (Black List)',
                'case_unit': 'Call for Action',
                'name': country,
                'details': 'FATF High-Risk Jurisdiction - subject to Call for Action and countermeasures. Verified Feb 2026 plenary.',
                'detail_page_url': URL,
                'link_kind': 'jurisdiction_risk',
                'scraped_at': now,
            })
        for country in FATF_GREY:
            w.writerow({**blank,
                'source_agency': 'Financial Action Task Force (FATF)',
                'source_list': 'Jurisdictions under Increased Monitoring (Grey List)',
                'case_unit': 'Increased Monitoring',
                'name': country,
                'details': 'FATF Grey List - under increased monitoring for AML/CFT deficiencies. Verified Feb 2026 plenary.',
                'detail_page_url': URL,
                'link_kind': 'jurisdiction_risk',
                'scraped_at': now,
            })
    print(f"FATF: {len(FATF_BLACK)} black + {len(FATF_GREY)} grey = {len(FATF_BLACK) + len(FATF_GREY)} rows")
