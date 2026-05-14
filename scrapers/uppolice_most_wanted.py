"""
UP Police Most Wanted scraper (#221).

Source: https://www.uppolice.gov.in/frmWantedperson.aspx

Page layout: a `<ul class="flex-container">` of `<li>` cards, each
containing a `<div class="profilepic">` with:
  <p class="name"><strong>Name :</strong> NAME </p>
  <p><strong>Father Name :</strong> FATHER </p>
  <p><strong>Address :</strong> ADDRESS </p>
plus a sibling `<div class="reward">` with the reward amount.

The generic engine produced nav-menu garbage on this page (it has 1k+
<li> items in its mega-menu, dwarfing the actual data); a custom
scraper is justified.

link_kind = 'uppolice_most_wanted'.
Sanity check: refuse to write CSV if extracted < 5 (page has had
30-40 entries historically; a sharp drop signals a layout change).
"""

import csv
import os
import re
from datetime import datetime

from scrapling import Fetcher

LIST_URL = "https://www.uppolice.gov.in/frmWantedperson.aspx"
BASE_URL = "https://www.uppolice.gov.in/"
EXPECTED_MIN = 5

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "uppolice_most_wanted.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]+?)\s*:\s*(.*?)\s*$")
INR_DIGITS = re.compile(r"[\d,]+")


def _clean(s):
    if not s:
        return ""
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_amount(text):
    """Return integer rupees or '' from a 'Reward : Rs 50,000' style cell."""
    if not text:
        return ""
    m = INR_DIGITS.search(text)
    if not m:
        return ""
    return m.group(0).replace(",", "")


def parse_profile(profilepic, reward_text, scraped_at):
    """Parse a profilepic div + its matching reward text into a record."""
    name = father = address = ""

    # Each label sits in a <p>. The label is in <strong>...</strong>
    # so we need get_all_text() to concatenate descendant text nodes.
    for p in (profilepic.find_all("p") or []):
        text_raw = p.get_all_text() if hasattr(p, "get_all_text") else p.text
        text = _clean(text_raw)
        if not text:
            continue
        m = LABEL_RE.match(text)
        if not m:
            continue
        label = m.group(1).lower()
        value = _clean(m.group(2))
        if "father" in label:
            father = value
        elif "address" in label:
            address = value
        elif "name" in label and not name:
            name = value

    if not name:
        return None

    reward = _parse_amount(reward_text) if reward_text else ""

    # Photo URL (relative -> absolute)
    photo_url = ""
    img = profilepic.find("img")
    if img is not None and hasattr(img, "attrib"):
        src = img.attrib.get("src", "")
        if src:
            photo_url = src if src.startswith("http") else BASE_URL.rstrip("/") + "/" + src.lstrip("/")

    return {
        "source_agency": "UP Police",
        "source_list": "Most Wanted",
        "case_unit": "",
        "name": name,
        "father_name": father,
        "date_of_birth": "",
        "gender": "",
        "address": address,
        "reward_amount": reward,
        "details": "",
        "has_document": "Yes" if photo_url else "No",
        "document_url": photo_url,
        "detail_page_url": LIST_URL,
        "interpol_notice_id": "",
        "link_kind": "uppolice_most_wanted",
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


def scrape():
    print(f"Fetching {LIST_URL}")
    page = Fetcher.get(LIST_URL, timeout=30, retries=1, retry_delay=0, verify=False)
    status = getattr(page, "status", None) or getattr(page, "status_code", None)
    if status is None or status >= 400:
        raise RuntimeError(f"UP Police HTTP {status}")

    # profilepic and reward divs appear in 1:1 order across the page.
    profiles = page.find_all("div.profilepic") or []
    rewards = page.find_all("div.reward") or []
    print(f"Found {len(profiles)} profilepic elements and {len(rewards)} reward elements")
    if not profiles:
        raise RuntimeError("No div.profilepic elements found - page layout changed")

    reward_texts = []
    for r in rewards:
        if r is None:
            reward_texts.append("")
            continue
        t = r.get_all_text() if hasattr(r, "get_all_text") else r.text
        reward_texts.append(_clean(t))
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for i, prof in enumerate(profiles):
        rew = reward_texts[i] if i < len(reward_texts) else ""
        rec = parse_profile(prof, rew, scraped_at)
        if rec is None:
            continue
        out.append(rec)

    if len(out) < EXPECTED_MIN:
        raise RuntimeError(
            f"UP Police: extracted {len(out)} records, below floor {EXPECTED_MIN}; "
            "refusing to write CSV (likely layout change)"
        )
    return out


def save_to_csv(records, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(records)} records to {out_path}")


def run():
    print("=" * 60)
    print("UP Police Most Wanted scraper (#221)")
    print("=" * 60)
    records = scrape()
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
