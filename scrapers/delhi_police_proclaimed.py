"""
Delhi Police Proclaimed Offenders.

Source: https://delhipolice.ncog.gov.in/Delhi_police/proclaimed.html

The page is a flat HTML document with no <table>; each entry is a
pair of <div class="row"><div class="...bolddiv">LABEL</div><div ...>
VALUE</div></div> blocks inside a <div class="border-div-down">
"card". Labels seen in the wild:

  * "Details of FIR"
  * "Name & Address of Proclaim Offender"
  * "Name of accused"
  * "Address" / "Adress"
  * "Name parentage & Address of accused"
  * "FIR No." / "FIR NO."

The parser walks every card div and collects label/value pairs, then
maps them to schema columns. Father-name and address are extracted
from the merged "Name & Address" / "S/o" / "r/o" prose where present.
"""

import csv
import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://delhipolice.ncog.gov.in/Delhi_police/proclaimed.html"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "delhi_police_proclaimed.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/120.0.0.0 Safari/537.36"}

# Labels that mean the value contains a name (sometimes with father +
# address mashed together).
NAME_LABELS = {
    "name & address of proclaim offender",
    "name of accused",
    "name parentage & address of accused",
    "name parentage and address of accused",
    "name parentage & address",
    "name parentage and address",
    "name & address",
    "name and address",
    "name of proclaim offender",
    "name of proclaimed offender",
    "name",
}
ADDR_LABELS = {"address", "adress"}
FIR_LABELS = {"details of fir", "fir no.", "fir no", "fir number"}

# Patterns inside the merged "Name + father + address" prose.
SO_RE = re.compile(
    r"\b(?:s/o|S/o|S/O|S\\o|son\s+of|w/o|W/o|d/o|D/o)\s+([^,()]+?)\s*"
    r"(?=\s*(?:r/o|R/o|R/O|residence|residing|address|,))",
    re.I,
)
RO_RE = re.compile(
    r"\b(?:r/o|R/o|R/O|resident\s+of|residing\s+at|address[: ]+)\s+(.+?)\s*$",
    re.I,
)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" .,;-")


def _split_name(value):
    """Given a merged "<NAME> s/o <FATHER> r/o <ADDR>" string, return
    (name, father, address)."""
    v = _clean(value)
    if not v:
        return "", "", ""
    father = ""
    address = ""
    m = SO_RE.search(v)
    if m:
        father = _clean(m.group(1))
    m2 = RO_RE.search(v)
    if m2:
        address = _clean(m2.group(1))
    # Name is everything before the first s/o or r/o marker.
    marker_pos = []
    for pat in (r"\bs/o\b", r"\bw/o\b", r"\bd/o\b", r"\br/o\b",
                 r"\bson\s+of\b", r"\bresident\s+of\b",
                 r"\bresiding\s+at\b"):
        m3 = re.search(pat, v, re.I)
        if m3:
            marker_pos.append(m3.start())
    if marker_pos:
        name = v[: min(marker_pos)].strip(" .,;-")
    else:
        name = v
    return name, father, address


def scrape():
    r = requests.get(LIST_URL, headers=UA, timeout=30, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"Delhi Police Proclaimed: status {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")

    # Each "card" is a <div class="border-div-down"> containing one
    # offender. Inside it are <div class="row"> pairs of label / value
    # cells.
    cards = soup.find_all("div", class_="border-div-down")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    for card in cards:
        fields = {}
        for row in card.find_all("div", class_="row"):
            cols = row.find_all("div", recursive=False)
            if len(cols) < 2:
                continue
            label = _clean(cols[0].get_text(" ", strip=True)).lower()
            value = _clean(cols[1].get_text(" ", strip=True))
            if not label or not value:
                continue
            fields[label] = value
        if not fields:
            continue
        # Pick a name field
        name_val = ""
        for k in fields:
            if k in NAME_LABELS:
                name_val = fields[k]
                break
        if not name_val:
            continue
        name, father, address = _split_name(name_val)
        # Standalone address overrides
        for k in ADDR_LABELS:
            if k in fields and not address:
                address = fields[k]
        # FIR details
        fir = ""
        for k in FIR_LABELS:
            if k in fields:
                fir = fields[k]
                break
        if not name:
            continue
        key = (name.lower(), father.lower(), fir.lower())
        if key in seen:
            continue
        seen.add(key)
        details_parts = []
        if fir:
            details_parts.append(f"FIR: {fir}")
        for k, v in fields.items():
            if k in NAME_LABELS or k in ADDR_LABELS or k in FIR_LABELS:
                continue
            details_parts.append(f"{k.title()}: {v}")
        out.append({
            "source_agency": "Delhi Police",
            "source_list":   "Proclaimed Offenders",
            "case_unit":     fir,
            "name":          name,
            "father_name":   father,
            "date_of_birth": "",
            "gender":        "",
            "address":       address,
            "reward_amount": "",
            "details":       " | ".join(details_parts),
            "has_document":  "No",
            "document_url":  "",
            "detail_page_url": LIST_URL,
            "interpol_notice_id": "",
            "link_kind":     "manual_discovery",
            "scraped_at":    scraped_at,
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
    print("Delhi Police Proclaimed Offenders")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("Delhi Police Proclaimed: 0 rows")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
