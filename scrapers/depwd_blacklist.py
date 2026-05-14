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

URL = "https://depwd.gov.in/blacklist"

r = requests.get(URL, timeout=30)
soup = BeautifulSoup(r.text, "html.parser")

table = soup.find("table")

rows = []

for tr in table.find_all("tr")[1:]:
    cols = tr.find_all("td")

    if len(cols) < 3:
        continue

    name = cols[0].get_text(" ", strip=True)
    date = cols[1].get_text(" ", strip=True)

    pdf = ""
    a = cols[2].find("a", href=True)

    if a:
        pdf = a["href"]

    rows.append({
    "source_agency": "DEPwD",
    "source_list": "Blacklisted Organizations",
    "case_unit": "",
    "name": name,
    "father_name": "",
    "date_of_birth": "",
    "gender": "",
    "address": "",
    "reward_amount": "",
    "details": f"Blacklisted organization. Date: {date}",
    "has_document": "yes" if pdf else "no",
    "document_url": pdf,
    "detail_page_url": URL,
    "interpol_notice_id": "",
    "link_kind": "pdf",
    "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "enrichment_status": "raw"
})

print(f"rows: {len(rows)}")

with open("data/depwd_blacklist.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=CSV_FIELDS
    )

    writer.writeheader()
    writer.writerows(rows)

print("saved.")
