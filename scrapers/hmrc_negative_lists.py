"""
HMRC negative lists scraper.

Sources:
  hmrc_tax_defaulters         -> "Current list of deliberate tax defaulters" (gov.uk)
  hmrc_tax_avoidance_promoters -> "Current list of named tax avoidance schemes,
                                  promoters, enablers and suppliers" (gov.uk)
Both are gov.uk HTML pages (server-rendered) — BeautifulSoup parses cleanly.
"""
import csv, os, re
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJECT_ROOT, "data")
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/124", "Accept-Language": "en-GB,en;q=0.9"}
HEADER = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url", "interpol_notice_id",
          "link_kind", "scraped_at", "enrichment_status"]

DEFAULTERS_URL = "https://www.gov.uk/government/publications/publishing-details-of-deliberate-tax-defaulters-pddd/current-list-of-deliberate-tax-defaulters"
PROMOTERS_URL  = "https://www.gov.uk/government/publications/named-tax-avoidance-schemes-promoters-enablers-and-suppliers/current-list-of-named-tax-avoidance-schemes-promoters-enablers-and-suppliers"


def fetch(url):
    r = requests.get(url, headers=HDRS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml"), url


def text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def rows_from_table(table):
    """Yield list-of-cells per data row of a govspeak table."""
    headers = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        vals = [text(c) for c in cells]
        if cells[0].name == "th" and not headers:
            headers = vals
            continue
        yield headers, vals


def scrape_defaulters():
    soup, page_url = fetch(DEFAULTERS_URL)
    body = soup.select_one(".govspeak") or soup
    out = []
    now = datetime.now(timezone.utc).isoformat()
    for table in body.find_all("table"):
        for headers, vals in rows_from_table(table):
            if not vals or all(not v for v in vals):
                continue
            d = {headers[i] if i < len(headers) and headers[i] else f"col{i+1}": vals[i] for i in range(len(vals))}
            # name = first non-empty column ("Name", "Name and / or trading style", etc.)
            name_key = next((k for k in d if k.lower().startswith("name")), None) or list(d.keys())[0]
            name = d.get(name_key, "")
            if not name or name.lower() in ("name", "total", "totals"):
                continue
            addr_key = next((k for k in d if "address" in k.lower()), None)
            address = d.get(addr_key, "") if addr_key else ""
            details_kv = " | ".join(f"{k}: {v}" for k, v in d.items() if v and k != name_key and k != addr_key)
            out.append({
                "source_agency": "HM Revenue & Customs (HMRC)",
                "source_list": "Deliberate Tax Defaulters",
                "case_unit": "", "name": name, "father_name": "",
                "date_of_birth": "", "gender": "", "address": address,
                "reward_amount": "", "details": details_kv,
                "has_document": "", "document_url": "",
                "detail_page_url": page_url,
                "interpol_notice_id": "", "link_kind": "",
                "scraped_at": now, "enrichment_status": "",
            })
    return out


def scrape_promoters():
    soup, page_url = fetch(PROMOTERS_URL)
    body = soup.select_one(".govspeak") or soup
    out = []
    now = datetime.now(timezone.utc).isoformat()
    # Each named entity is introduced by an h3 followed by one or two tables.
    # Walk siblings under govspeak, group by h3.
    current_name = None
    current_anchor = None
    for el in body.descendants:
        if getattr(el, "name", None) == "h3":
            current_name = text(el)
            current_anchor = el.get("id", "")
        elif getattr(el, "name", None) == "table" and current_name:
            d = {}
            for headers, vals in rows_from_table(el):
                # 2-column tables: key/value style
                if len(vals) == 2:
                    d[vals[0]] = vals[1]
                else:
                    for i, v in enumerate(vals):
                        k = headers[i] if i < len(headers) and headers[i] else f"col{i+1}"
                        d[k] = v
            if not d:
                continue
            addr = d.get("Address") or d.get("Registered office") or d.get("Principal office") or ""
            details_kv = " | ".join(f"{k}: {v}" for k, v in d.items() if v and not k.lower().startswith("address"))
            anchor = (page_url + "#" + current_anchor) if current_anchor else page_url
            out.append({
                "source_agency": "HM Revenue & Customs (HMRC)",
                "source_list": "Named Tax Avoidance Promoters, Enablers & Suppliers",
                "case_unit": "", "name": current_name, "father_name": "",
                "date_of_birth": "", "gender": "", "address": addr,
                "reward_amount": "", "details": details_kv,
                "has_document": "", "document_url": "",
                "detail_page_url": anchor,
                "interpol_notice_id": "", "link_kind": "",
                "scraped_at": now, "enrichment_status": "",
            })
            # reset name AFTER consuming the first table for that name to avoid
            # the same name being attached to "evidence" tables that follow.
            current_name = None
            current_anchor = None
    return out


def write_csv(rows, fname):
    path = os.path.join(DATA, fname)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    return path, len(rows)


def main():
    d = scrape_defaulters()
    p1, n1 = write_csv(d, "hmrc_tax_defaulters.csv")
    print(f"[defaulters] {n1} rows -> {p1}")
    pr = scrape_promoters()
    p2, n2 = write_csv(pr, "hmrc_tax_avoidance_promoters.csv")
    print(f"[promoters]  {n2} rows -> {p2}")


if __name__ == "__main__":
    main()
