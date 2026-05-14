"""
NIA Most Wanted scraper.

Source: https://www.nia.gov.in/most-wanted

Card grid + Bootstrap detail modals, paginated across 17 pages
(?page=0..16). Each modal contains one record. The per-record HTML
is fully present in the static page source — no JavaScript needed.

Per-record fields are extracted by iterating div.row blocks inside
each div.wanted-modal-card, keying off the label string in
div.col-4.lab. The reward amount (when present) sits in a separate
div.rewardd outside the row grid.

This scraper:
  - walks all 17 pages with a 2s delay between fetches
  - hard-fails if the collected count != EXPECTED_TOTAL (332)
  - keeps cleaning helpers self-contained (no common/ extraction yet)
"""

import csv
import os
import re
import time
from datetime import datetime

from scrapling import Fetcher

BASE_URL = "https://www.nia.gov.in/most-wanted"
DELAY = 2          # seconds between page fetches
NUM_PAGES = 17     # 0..16 inclusive
EXPECTED_TOTAL = 332

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "nia_most_wanted.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

MISSING_VARIANTS = {"-", "--", "n/a", "na", "unknown", "none"}
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")


# ============================================================
# CLEANING HELPERS  (self-contained per "no premature abstraction")
# ============================================================
def clean_missing(value):
    """Collapse known 'missing' placeholders to ''."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in MISSING_VARIANTS or s == "":
        return ""
    return s


# NIA's div.rewardd is prose, not a structured number. The shared
# clean_amount() (still used by other scrapers) strips all non-digits and
# concatenates them — for cells like "05 lakh declared vide office order
# dtd 21.03.2025" that mashes the amount, RC numbers, and dates into
# nonsense composites. clean_nia_reward() below replaces it on this
# source only by matching the first '<n> lakh|crore' pattern and
# applying the Indian magnitude multiplier.
INR_AMOUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(lakh|lakhs|crore|crores)",
    re.IGNORECASE,
)


def clean_nia_reward(value):
    """
    Parse NIA's prose-style reward cell into a rupee integer.

    Matches the FIRST '<number> lakh|crore' pattern in the string and
    multiplies by 100_000 (lakh) or 10_000_000 (crore). Returns '' if no
    recognisable amount is present — better than emitting a digit-mash
    composite that downstream consumers would treat as a real amount.
    """
    if not value:
        return ""
    m = INR_AMOUNT_RE.search(value)
    if m is None:
        return ""
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("lakh"):
        return int(num * 100_000)
    if unit.startswith("crore"):
        return int(num * 10_000_000)
    return ""


def clean_date(value):
    """ISO date string or ''. Strict — rejects approximate/future/pre-1900."""
    s = clean_missing(value)
    if not s:
        return ""
    parsed = None
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(s, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        return ""
    today = datetime.now().date()
    if parsed > today or parsed.year < 1900:
        return ""
    return parsed.isoformat()


def normalise_aliases(raw):
    """
    NIA's Aliases field has inconsistent spacing around '@'
    (sometimes '@', sometimes ' @ ', sometimes '@ '). Collapse all
    runs of optional-whitespace + @ + optional-whitespace to ' @ ',
    and strip any leading '@ '.
    """
    if not raw:
        return ""
    s = re.sub(r"\s*@\s*", " @ ", raw).strip()
    if s.startswith("@ "):
        s = s[2:]
    return s


# ============================================================
# RECORD EXTRACTION
# ============================================================
def extract_fields(card):
    """Return a {label_string: value_string} dict from one wanted-modal-card."""
    fields = {}
    for row in card.find_all("div.row"):
        lab = row.find("div.col-4.lab")
        val = row.find("div.col-8.val")
        if lab is None or val is None:
            continue
        label = (lab.text or "").strip().rstrip(":").strip()
        # val sometimes wraps an <a>; .get_all_text() picks up either.
        value_text = (val.get_all_text() or "").strip() if hasattr(val, "get_all_text") else (val.text or "").strip()
        fields[label] = value_text
    return fields


def extract_reward(card):
    """Return the reward amount text (everything after 'Reward :' inside div.rewardd)."""
    rewardd = card.find("div.rewardd")
    if rewardd is None:
        return ""
    full = (rewardd.get_all_text() or "").strip() if hasattr(rewardd, "get_all_text") else (rewardd.text or "").strip()
    return re.sub(r"^Reward\s*:\s*", "", full).strip()


def build_record(fields, reward_raw, scraped_at):
    """Map NIA labels to our 17-column schema."""
    name_raw = clean_missing(fields.get("Name", ""))
    aliases_norm = normalise_aliases(fields.get("Aliases", ""))

    if name_raw and aliases_norm:
        name = f"{name_raw} @ {aliases_norm}"
    else:
        name = name_raw or aliases_norm

    # Compose `details` from Accused Status + Organization (when populated)
    detail_parts = []
    accused = clean_missing(fields.get("Accused Status", ""))
    org = clean_missing(fields.get("Organization", ""))
    if accused:
        detail_parts.append(f"Accused Status: {accused}")
    if org:
        detail_parts.append(f"Organization: {org}")
    details = " | ".join(detail_parts)

    # case_unit can hold multiple RC numbers separated on the source page
    # by inter-anchor whitespace + ', ' + more whitespace, so the captured
    # value contains literal '\n' characters. Collapse all whitespace runs
    # to single spaces so downstream consumers don't see embedded newlines.
    case_unit = clean_missing(fields.get("Wanted in", ""))
    if case_unit:
        case_unit = re.sub(r"\s+", " ", case_unit).strip()

    return {
        "source_agency": "NIA",
        "source_list": "Most Wanted",
        "case_unit": case_unit,
        "name": name,
        "father_name": clean_missing(fields.get("Parentage", "")),
        "date_of_birth": clean_date(fields.get("Age/DOB (Approx)", "")),
        "gender": "",
        "address": clean_missing(fields.get("Address", "")),
        "reward_amount": clean_nia_reward(reward_raw),
        "details": details,
        "has_document": "No",
        "document_url": "",
        "detail_page_url": "",
        "interpol_notice_id": "",
        "link_kind": "nia_most_wanted",
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


# ============================================================
# SCRAPE + SAVE
# ============================================================
def scrape_all_pages():
    """Walk pages 0..16, parse every wanted-modal-card, return all records."""
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for p in range(NUM_PAGES):
        url = BASE_URL if p == 0 else f"{BASE_URL}?page={p}"
        print(f"  page {p+1}/{NUM_PAGES}: {url}")
        page = Fetcher.get(url)
        cards = page.find_all("div.wanted-modal-card")
        print(f"    found {len(cards)} cards")
        for card in cards:
            fields = extract_fields(card)
            reward_raw = extract_reward(card)
            records.append(build_record(fields, reward_raw, scraped_at))
        if p < NUM_PAGES - 1:
            time.sleep(DELAY)
    return records


def save_to_csv(records, output_file):
    """Write records to CSV. Creates parent dirs if needed."""
    if not records:
        print("No records to save.")
        return
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in records:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(records)} records to {output_file}")


# ============================================================
# MAIN
# ============================================================
def run():
    print("=" * 60)
    print("NIA Most Wanted scraper")
    print("=" * 60)

    records = scrape_all_pages()
    print(f"\nTotal records collected: {len(records)}")

    if len(records) != EXPECTED_TOTAL:
        raise RuntimeError(
            f"Sanity check failed: collected {len(records)} records but "
            f"expected exactly {EXPECTED_TOTAL} (verified during recon by "
            f"walking all {NUM_PAGES} pages). NOT writing CSV. "
            f"Re-walk pages or inspect the parser."
        )

    print("-" * 60)
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
