import csv
import requests
from bs4 import BeautifulSoup
from datetime import datetime

CSV_FIELDS = [
    "source_agency",
    "source_list",
    "case_unit",
    "name",
    "father_name",
    "date_of_birth",
    "gender",
    "address",
    "reward_amount",
    "details",
    "has_document",
    "document_url",
    "detail_page_url",
    "interpol_notice_id",
    "link_kind",
    "scraped_at",
    "enrichment_status",
]

URL = "https://socialjustice.gov.in/common/73590"

r = requests.get(URL, timeout=30)

soup = BeautifulSoup(r.text, "html.parser")

tables = soup.find_all("table")

main_table = tables[0]

rows = []

for tr in main_table.find_all("tr")[1:]:

    cols = tr.find_all("td")

    if len(cols) < 3:
        continue

    name = cols[1].get_text(" ", strip=True)

    action = cols[2].get_text(" ", strip=True)

    pdf = ""

    a = cols[2].find("a", href=True)

    if a:
        pdf = requests.compat.urljoin(URL, a["href"])

    rows.append({
        "source_agency": "Social Justice",
        "source_list": "Grants Suspended / Blacklisted NGOs",
        "case_unit": "",
        "name": name,
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": action,
        "has_document": "yes" if pdf else "no",
        "document_url": pdf,
        "detail_page_url": URL,
        "interpol_notice_id": "",
        "link_kind": "pdf" if pdf else "",
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "enrichment_status": "raw"
    })

print("rows:", len(rows))

with open(
    "data/socialjustice_blacklisted_ngos.csv",
    "w",
    newline="",
    encoding="utf-8"
) as f:

    writer = csv.DictWriter(
        f,
        fieldnames=CSV_FIELDS
    )

    writer.writeheader()

    writer.writerows(rows)

print("saved.")
