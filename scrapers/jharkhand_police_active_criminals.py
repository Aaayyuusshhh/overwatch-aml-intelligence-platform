"""
Jharkhand Police — Active Criminals.

Source: https://jhpolice.gov.in/active-criminals (Drupal-views listing).

The page renders a 4-column grid of criminal "cards" with paginated
results at ?page=0, ?page=1, … Each card is a single `<td>` whose
text follows the format:

  <Name> Alias: <Alias> Age: <N> Sex: <M|F> Position: <Bail|Jail|Desert>
  Activity: <Crime>

We walk pages until the response length collapses to the empty-result
template (~36 KB). Names appear in both Hindi (Devanagari) and English
across pages.
"""

import csv
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://jhpolice.gov.in/active-criminals"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "jharkhand_police_active_criminals.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/120.0.0.0 Safari/537.36",
      "Accept": "text/html,*/*;q=0.8"}

# Empty-results page length (the template without any view rows).
EMPTY_PAGE_LEN_THRESHOLD = 40_000


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" .,;:-")


def _parse_card(cell_text, img_src):
    """Pull (name, alias, age, sex, position, activity) out of a card."""
    t = _clean(cell_text)
    if not t:
        return None
    # Each card text always has "Alias:" — anchor on it.
    parts = re.split(r"\s*Alias\s*:\s*", t, maxsplit=1)
    name = _clean(parts[0])
    rest = parts[1] if len(parts) > 1 else ""
    fields = {"alias": "", "age": "", "sex": "",
              "position": "", "activity": ""}
    # Each subsequent label is at the start of a token cluster.
    for label in ("Age", "Sex", "Position", "Activity"):
        m = re.search(rf"{label}\s*:\s*(.*?)\s*(?=\b(Age|Sex|Position|Activity)\s*:|$)",
                       rest, re.DOTALL)
        if m:
            fields[label.lower()] = _clean(m.group(1))
    # Alias = everything before Age: in `rest`
    m = re.search(r"^(.*?)\s*(?=\bAge\s*:|$)", rest, re.DOTALL)
    if m:
        fields["alias"] = _clean(m.group(1))
    return {
        "name":     name,
        "alias":    fields["alias"],
        "age":      fields["age"],
        "sex":      fields["sex"],
        "position": fields["position"],
        "activity": fields["activity"],
        "photo":    img_src,
    }


def scrape():
    sess = requests.Session()
    sess.headers.update(UA)
    sess.verify = False

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    for page in range(0, 30):                  # safety cap
        url = f"{LIST_URL}?page={page}"
        try:
            r = sess.get(url, timeout=30)
        except Exception as e:
            print(f"  page {page}: {type(e).__name__}: {e}")
            time.sleep(2)
            continue
        if r.status_code != 200:
            print(f"  page {page}: status {r.status_code}")
            break
        if page > 0 and len(r.content) <= EMPTY_PAGE_LEN_THRESHOLD:
            print(f"  page {page}: empty-template ({len(r.content)} bytes), stop")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for t in soup.find_all("table"):
            for tr in t.find_all("tr"):
                for td in tr.find_all(["td", "th"]):
                    text = td.get_text(" ", strip=True)
                    if not text or "Alias" not in text:
                        continue
                    img = td.find("img")
                    img_src = ""
                    if img and img.get("src"):
                        img_src = urljoin(url, img["src"])
                    parsed = _parse_card(text, img_src)
                    if not parsed or not parsed["name"]:
                        continue
                    # dedup
                    key = (parsed["name"].lower(),
                           parsed["alias"].lower(),
                           parsed["activity"].lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    detail_parts = []
                    if parsed["alias"]:
                        detail_parts.append(f"Alias: {parsed['alias']}")
                    if parsed["age"]:
                        detail_parts.append(f"Age: {parsed['age']}")
                    if parsed["position"]:
                        detail_parts.append(f"Position: {parsed['position']}")
                    if parsed["activity"]:
                        detail_parts.append(f"Activity: {parsed['activity']}")
                    out.append({
                        "source_agency": "Jharkhand Police (JP)",
                        "source_list":   "Active Criminals",
                        "case_unit":     "",
                        "name":          parsed["name"],
                        "father_name":   "",
                        "date_of_birth": "",
                        "gender":        parsed["sex"],
                        "address":       "",
                        "reward_amount": "",
                        "details":       " | ".join(detail_parts),
                        "has_document":  "Yes" if parsed["photo"] else "No",
                        "document_url":  parsed["photo"],
                        "detail_page_url": LIST_URL,
                        "interpol_notice_id": "",
                        "link_kind":     "manual_discovery",
                        "scraped_at":    scraped_at,
                        "enrichment_status": "",
                    })
                    added += 1
        print(f"  page {page}: +{added}  total={len(out)}")
        time.sleep(1.0)
        if added == 0 and page > 0:
            break
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
    print("Jharkhand Police — Active Criminals")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("Jharkhand Police: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
