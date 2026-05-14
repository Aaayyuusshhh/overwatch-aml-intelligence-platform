"""
UNSC Consolidated Sanctions List (#32).

Source: a local HTML at data/consolidatedLegacyByNAME.html, manually
saved from the UN Security Council site. The HTML contains 4 tables:
  table 0: print-to-pdf chrome
  table 1: title block ("United Nations Security Council Consolidated List")
  table 2: Individuals  (one entry per <tr>, single TD with all fields)
  table 3: Entities     (one entry per <tr>, single TD with all fields)

Each entry's text follows a stable labelled format:

  Individuals: "<ID> Name: 1: X 2: Y 3: Z 4: W [Name (original script): …]
    Title: … Designation: … DOB: … POB: … Good quality a.k.a.: … Low
    quality a.k.a.: … Nationality: … Passport no: … National
    identification no: … Address: … Listed on: <date> Other
    information: …"

  Entities: "<ID> Name: <NAME> A.k.a.: … F.k.a.: … Address: … Listed
    on: <date> Other information: …"

ID prefix encodes the committee (QDi=Al-Qaida individual,
QDe=Al-Qaida entity, IRe=Iran entity, SDi=Somalia individual, etc.)
and is preserved in case_unit. Type comes from the 3rd letter:
  'i' -> Individual, 'e' -> Entity.
"""

import csv
import os
import re
from datetime import datetime

from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(PROJECT_ROOT, "data", "consolidatedLegacyByNAME.html")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "unsc_consolidated_sanctions_32.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# Field labels (in display order); patterns capture the value up to the
# next label or end-of-string.
INDIVIDUAL_LABELS = [
    "Name", "Name (original script)", "Title", "Designation",
    "DOB", "POB", "Good quality a.k.a.", "Low quality a.k.a.",
    "Nationality", "Passport no", "National identification no",
    "Address", "Listed on", "Other information",
]
ENTITY_LABELS = [
    "Name", "A.k.a.", "F.k.a.", "Address", "Listed on", "Other information",
]

# Compile one regex per label that grabs everything until the next label
# (or end). Labels are sorted longest-first so "Name (original script):"
# matches before "Name:".
def _build_field_regex(labels):
    # Escape labels for regex, sort by length descending so the longer
    # ones (Name (original script)) anchor first.
    escaped = sorted([re.escape(l) for l in labels], key=len, reverse=True)
    alt = "|".join(escaped)
    return re.compile(r"\b(" + alt + r"):\s*", re.I), escaped


_IND_LABEL_RE, _IND_LABELS = _build_field_regex(INDIVIDUAL_LABELS)
_ENT_LABEL_RE, _ENT_LABELS = _build_field_regex(ENTITY_LABELS)

ID_RE = re.compile(r"^\s*([A-Z]{2,4}[a-z]?\.?\d+[A-Za-z]?)\s+", re.I)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _na_strip(v):
    """UNSC uses literal 'na' to mean empty — drop those."""
    if not v:
        return ""
    if v.lower().strip() in {"na", "n/a", "none"}:
        return ""
    return v


def _split_fields(text, label_re):
    """Walk through labelled fields and return dict label -> value."""
    fields = {}
    matches = list(label_re.finditer(text))
    if not matches:
        return fields
    for i, m in enumerate(matches):
        label = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        val = _clean(text[start:end])
        fields[label.lower()] = val
    return fields


def _individual_name(fields):
    """Combine Name: 1: X 2: Y 3: Z 4: W into a single name string."""
    raw = fields.get("name", "")
    if not raw:
        return ""
    # The "Name:" value usually starts "1: X 2: Y 3: Z 4: W". Split on
    # the n-numbered markers.
    parts = re.findall(r"\d+:\s*([^\d:]+?)(?=\s+\d+:|$)", raw)
    parts = [p.strip() for p in parts if p.strip() and p.strip().lower() != "na"]
    if parts:
        return " ".join(parts)
    return _na_strip(raw)


def _parse_entry(text, type_letter):
    """Return a record dict for one entry, or None to skip."""
    text = _clean(text)
    if not text or "Name:" not in text:
        return None
    m = ID_RE.match(text)
    if not m:
        return None
    ref_id = m.group(1).rstrip(".")
    body = text[m.end():]

    if type_letter == "i":
        fields = _split_fields(body, _IND_LABEL_RE)
        name = _individual_name(fields)
        dob = _na_strip(fields.get("dob", ""))
    else:
        fields = _split_fields(body, _ENT_LABEL_RE)
        name = _na_strip(fields.get("name", ""))
        dob = ""
    if not name:
        return None

    nationality = _na_strip(fields.get("nationality", ""))
    address = _na_strip(fields.get("address", ""))
    passport = _na_strip(fields.get("passport no", ""))
    natid = _na_strip(fields.get("national identification no", ""))
    aka_good = _na_strip(fields.get("good quality a.k.a.", ""))
    aka_low = _na_strip(fields.get("low quality a.k.a.", ""))
    aka_ent = _na_strip(fields.get("a.k.a.", ""))
    fka_ent = _na_strip(fields.get("f.k.a.", ""))
    listed_on = _na_strip(fields.get("listed on", ""))
    designation = _na_strip(fields.get("designation", ""))
    title = _na_strip(fields.get("title", ""))
    pob = _na_strip(fields.get("pob", ""))

    detail_parts = []
    type_label = "Individual" if type_letter == "i" else "Entity"
    detail_parts.append(f"Type: {type_label}")
    detail_parts.append(f"UN Ref: {ref_id}")
    aliases = " | ".join(filter(None, [aka_good, aka_low, aka_ent, fka_ent]))
    if aliases:
        detail_parts.append(f"Aliases: {aliases}")
    if title:
        detail_parts.append(f"Title: {title}")
    if designation:
        detail_parts.append(f"Designation: {designation}")
    if pob:
        detail_parts.append(f"POB: {pob}")
    if nationality:
        detail_parts.append(f"Nationality: {nationality}")
    if passport:
        detail_parts.append(f"Passport: {passport}")
    if natid:
        detail_parts.append(f"National ID: {natid}")
    if listed_on:
        # The OCR-style "Listed on" sometimes carries the amendment
        # history; trim to the first date.
        listed_first = re.sub(r"\s*\(.*$", "", listed_on)
        detail_parts.append(f"Listed: {listed_first}")

    return {
        "ref_id": ref_id,
        "type_letter": type_letter,
        "name": name,
        "address": address if address else nationality,
        "dob": dob,
        "details": " | ".join(detail_parts),
    }


def scrape():
    if not os.path.exists(HTML_PATH):
        raise RuntimeError(f"UNSC: HTML missing at {HTML_PATH}")
    with open(HTML_PATH, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")
    tables = soup.find_all("table")
    if len(tables) < 4:
        raise RuntimeError(f"UNSC: expected 4 tables, found {len(tables)}")
    ind_table, ent_table = tables[2], tables[3]

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    indiv = ents = 0
    for tr in ind_table.find_all("tr"):
        txt = tr.get_text(" ", strip=True)
        rec = _parse_entry(txt, "i")
        if rec:
            indiv += 1
            out.append(_to_record(rec, scraped_at))
    for tr in ent_table.find_all("tr"):
        txt = tr.get_text(" ", strip=True)
        rec = _parse_entry(txt, "e")
        if rec:
            ents += 1
            out.append(_to_record(rec, scraped_at))
    print(f"  parsed {indiv} individuals + {ents} entities = {len(out)}")
    return out


def _to_record(r, scraped_at):
    return {
        "source_agency": "UNSC",
        "source_list": "Consolidated Sanctions List",
        "case_unit": r["ref_id"],
        "name": r["name"],
        "father_name": "",
        "date_of_birth": r["dob"],
        "gender": "",
        "address": r["address"],
        "reward_amount": "",
        "details": r["details"],
        "has_document": "No",
        "document_url": "",
        "detail_page_url": "",
        "interpol_notice_id": "",
        "link_kind": "manual_discovery",
        "scraped_at": scraped_at,
        "enrichment_status": "",
    }


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
    print("UNSC Consolidated Sanctions List (#32)")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
