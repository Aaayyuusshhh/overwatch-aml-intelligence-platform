"""
CBI Most Wanted - Announced Rewards scraper.

Scrapes person records from the CBI public list and old-records pages,
extracts each detail page, and writes a flat CSV.

Library: Scrapling 0.4.7  (Fetcher.get is a class method; selector API
uses find() / find_all() with CSS selectors.)
"""

import csv
import os
import re
import time
from datetime import datetime

from scrapling import Fetcher

# ============================================================
# CONFIGURATION
# ============================================================
LIST_URL = "https://cbi.gov.in/announce-rewards"
OLD_LIST_URL = "https://cbi.gov.in/old-records"
BASE_URL = "https://cbi.gov.in"

# Resolve paths relative to the project root (parent of this file's folder)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "cbi_announced_rewards.csv")

DELAY = 2  # seconds between requests

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# Strings used by the source pages to mean "no value".
MISSING_VARIANTS = {"-", "--", "n/a", "na", "unknown", "none"}

# Recognised CBI date formats seen on the source pages.
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")

# A clean case ID looks like "RC0202023A0006" / "RCVSP1998A0008" / "RC.32/S/2014-SCB/Kol".
CASE_ID_PATTERN = re.compile(r"\bRC[.A-Z0-9][.A-Z0-9/\-]*", re.IGNORECASE)


# ============================================================
# CLEANING HELPERS — normalise scraped strings before saving
# ============================================================
def clean_missing(value):
    """Collapse known 'missing' placeholders to ''. Returns trimmed string otherwise."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in MISSING_VARIANTS or s == "":
        return ""
    return s


def clean_amount(value):
    """Reduce a reward string to an int, or '' if no digits / unrecoverable."""
    s = clean_missing(value)
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    return int(digits)


def clean_date(value):
    """Return ISO date string. Reject empty, future, pre-1900, or unparseable values."""
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


def clean_case_unit(value):
    """
    Source pages put one of three things in the case-unit span:
      1. A clean case ID (e.g., 'RC0202023A0006')         -> keep as-is
      2. Boilerplate that contains the case ID            -> extract the RC token
      3. A non-ID label (e.g., 'Wanted Accused person')   -> ''
    """
    s = clean_missing(value)
    if not s:
        return ""

    # Case 1: short and ID-shaped.
    if len(s) <= 40 and re.fullmatch(r"RC[.\sA-Z0-9/\-]+", s, re.IGNORECASE):
        return s

    # Case 2: try to pull the RC token out of a boilerplate string.
    match = CASE_ID_PATTERN.search(s)
    if match:
        candidate = match.group(0).rstrip(".-")
        # Reject matches that are basically just "RC" — too short to be useful.
        if len(candidate) >= 6:
            return candidate

    # Case 3: nothing usable.
    return ""


# ============================================================
# SMALL DOM HELPERS — each does ONE thing
# ============================================================
def safe_text(element):
    """Return stripped text from a Scrapling element, '' if None/empty."""
    if element is None:
        return ""
    txt = element.text
    if txt is None:
        return ""
    return str(txt).strip()


def absolute_url(href):
    """Turn a relative href into a full https://cbi.gov.in/... URL."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


def fetch(url):
    """Fetch a URL and return a Scrapling response, or None on failure."""
    try:
        return Fetcher.get(url)
    except Exception as e:
        print(f"  FETCH ERROR ({url}): {e}")
        return None


# ============================================================
# PHASE 1: COLLECT DETAIL PAGE URLS FROM A LIST PAGE
# ============================================================
def get_detail_urls(list_url):
    """
    Visit a list page and collect all detail-page URLs from the
    person cards (div.mostWnatedBox > a).
    """
    print(f"Fetching list page: {list_url}")
    page = fetch(list_url)
    if page is None:
        return []

    urls = []
    cards = page.find_all("div.mostWnatedBox")
    print(f"  Found {len(cards)} person cards")

    for card in cards:
        link = card.find("a")
        if link is None:
            continue
        href = link.attrib.get("href", "")
        if "announce-reward-detail" not in href:
            continue
        urls.append(absolute_url(href))

    return urls


# ============================================================
# PHASE 2: PARSE FIELDS FROM ONE DETAIL PAGE
# ============================================================
def extract_case_unit(page):
    """
    Pull the case-unit string from div.oldRecordHeader > h3 (> span)
    and run it through clean_case_unit to strip boilerplate / labels.
    """
    header = page.find("div.oldRecordHeader")
    if header is None:
        return ""
    span = header.find("h3 span")
    raw = safe_text(span) if span is not None else safe_text(header.find("h3"))
    return clean_case_unit(raw)


def extract_document(page):
    """Find an attached document link on the page. Returns (has_document, document_url)."""
    # Try direct .pdf-suffixed link first, then any link mentioning pdf.
    link = page.find("a[href$='.pdf']")
    if link is None:
        link = page.find("a[href*='.pdf']")
    if link is None:
        return "No", ""
    href = link.attrib.get("href", "")
    if not href:
        return "No", ""
    return "Yes", absolute_url(href)


def map_label_to_field(label):
    """
    Map a human-readable label to one of our schema keys.

    Normalisation: trim whitespace, strip a trailing colon, lowercase.
    CBI is inconsistent — 'Branch Name:' on /old-records vs 'Branch Name'
    elsewhere — so the colon must be removed before substring matching.

    Rule order matters: 'Father Name' and 'Branch Name' both contain 'name',
    so the specific rules must be checked BEFORE the generic 'name' rule.
    """
    label = (label or "").strip().rstrip(":").strip().lower()
    if not label:
        return None
    if "father" in label:
        return "father_name"
    if "branch" in label:
        return "case_unit"            # 'Branch Name' on /old-records
    if "name" in label:
        return "name"
    if "birth" in label:
        return "date_of_birth"
    if "gender" in label or "sex" in label:
        return "gender"
    if "address" in label:
        return "address"
    if "reward" in label:
        return "reward_amount"
    if "charges" in label or "detail" in label or "remark" in label or "description" in label:
        return "details"              # 'Charges' on /old-records
    return None


def extract_box_fields(page):
    """
    Walk every div.box on the page. Each box has two <p> tags:
    label and value. Return a dict of {schema_key: value}.
    """
    extracted = {}
    boxes = page.find_all("div.box")
    for box in boxes:
        ps = box.find_all("p")
        if len(ps) < 2:
            continue
        label = safe_text(ps[0])
        value = safe_text(ps[1])
        if not label:
            continue
        key = map_label_to_field(label)
        if key and value:
            extracted[key] = value
    return extracted


def extract_person_data(url):
    """
    Visit one detail page and return a record dict matching CSV_FIELDS.
    Missing fields are returned as empty strings.
    """
    print(f"  Scraping: {url}")
    record = {
        "source_agency": "CBI",
        "source_list": "Most Wanted - Announced Rewards",
        "case_unit": "",
        "name": "",
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": "",
        "has_document": "No",
        "document_url": "",
        "detail_page_url": url,
        "interpol_notice_id": "",
        "link_kind": "cbi_detail_page",
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "enrichment_status": "none",
    }

    page = fetch(url)
    if page is None:
        return record  # Return shell record; caller can decide what to do.

    record["case_unit"] = extract_case_unit(page)
    record.update(extract_box_fields(page))
    record["has_document"], record["document_url"] = extract_document(page)

    # Normalise scraped values to schema conventions.
    record["name"] = clean_missing(record["name"])
    record["father_name"] = clean_missing(record["father_name"])
    record["address"] = clean_missing(record["address"])
    record["details"] = clean_missing(record["details"])
    record["gender"] = clean_missing(record["gender"])
    record["date_of_birth"] = clean_date(record["date_of_birth"])
    record["reward_amount"] = clean_amount(record["reward_amount"])

    return record


# ============================================================
# PHASE 2b: PARSE ONE INLINE RECORD ON /old-records
# ============================================================
# /old-records is structurally different from /announce-rewards:
# the entire dataset is rendered inline on a single page as a stack of
# div.oldRecordContent blocks (one per person). There are NO per-person
# detail URLs to follow. The fields inside each block use the same
# div.box label/value structure as /announce-reward-detail/, so we reuse
# extract_box_fields() directly. case_unit comes from the 'Branch Name'
# label here (mapped via map_label_to_field), not from a header span.
def extract_inline_record(block):
    """Build a record dict from one div.oldRecordContent block."""
    record = {
        "source_agency": "CBI",
        "source_list": "Most Wanted - Announced Rewards",
        "case_unit": "",
        "name": "",
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": "",
        "has_document": "No",
        "document_url": "",
        "detail_page_url": "",
        "interpol_notice_id": "",
        # link_kind = 'cbi_inline_record':
        # Some of these rows end up with name='' because CBI publishes the
        # literal string 'UNKNOWN' as a placeholder for unidentified
        # suspects, and clean_missing() normalises 'UNKNOWN' to ''. These
        # rows remain linkable via document_url (each has a reward PDF).
        # This is intentional, not a parser bug.
        "link_kind": "cbi_inline_record",
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "enrichment_status": "none",
    }

    record.update(extract_box_fields(block))
    record["has_document"], record["document_url"] = extract_document(block)

    # No per-record HTML page exists for /old-records, so detail_page_url
    # falls back to document_url when a PDF link is present.
    if record["document_url"]:
        record["detail_page_url"] = record["document_url"]

    # Normalise. We do NOT run clean_case_unit here — the value is a branch
    # name like "AC-I Delhi", not an RC-style case ID, and the RC-extraction
    # regex would erase it.
    record["name"] = clean_missing(record["name"])
    record["father_name"] = clean_missing(record["father_name"])
    record["address"] = clean_missing(record["address"])
    record["details"] = clean_missing(record["details"])
    record["gender"] = clean_missing(record["gender"])
    # case_unit semantics differ by link_kind:
    #   cbi_detail_page  -> RC IDs (e.g. 'RC0202023A0006') from oldRecordHeader
    #   cbi_inline_record -> branch names (e.g. 'AC-I Delhi') from 'Branch Name'
    # /old-records doesn't expose RC IDs, so the branch name is the
    # closest identifier this source provides. Intentional difference.
    record["case_unit"] = clean_missing(record["case_unit"])
    record["date_of_birth"] = clean_date(record["date_of_birth"])
    record["reward_amount"] = clean_amount(record["reward_amount"])

    return record


def scrape_old_records():
    """Fetch /old-records once and parse every inline record block."""
    print(f"\nFetching list page: {OLD_LIST_URL}")
    page = fetch(OLD_LIST_URL)
    if page is None:
        return []
    blocks = page.find_all("div.oldRecordContent")
    print(f"  Found {len(blocks)} inline record blocks")
    records = []
    for i, block in enumerate(blocks, start=1):
        try:
            records.append(extract_inline_record(block))
        except Exception as e:
            print(f"  [/old-records {i}] ERROR: {e}")
    return records


# ============================================================
# PHASE 3: WRITE CSV
# ============================================================
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
            # Defensive: only write known keys.
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})

    print(f"\nSaved {len(records)} records to {output_file}")


# ============================================================
# MAIN
# ============================================================
def run():
    print("=" * 60)
    print("CBI Announced Rewards scraper")
    print("=" * 60)

    records = []

    # Flow 1: /announce-rewards — card grid -> per-person detail pages.
    urls = get_detail_urls(LIST_URL)
    # Deduplicate while preserving order.
    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    print(f"\n[/announce-rewards] {len(urls)} detail URLs")
    for i, url in enumerate(urls, start=1):
        print(f"[{i}/{len(urls)}]", end=" ")
        try:
            records.append(extract_person_data(url))
        except Exception as e:
            print(f"  ERROR on {url}: {e}")
        time.sleep(DELAY)

    # Flow 2: /old-records — single page, inline record blocks, no detail URLs.
    time.sleep(DELAY)
    records.extend(scrape_old_records())

    print("-" * 60)
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
