#!/usr/bin/env python3
"""Scraper: National Investigation Agency (NIA) — Most Wanted Photos.

Paginated card/modal layout at https://nia.gov.in/most-wanted-photos?page=N.
Each person = div.wanted-modal-card containing label/value rows
(div.col-4.lab / div.col-8.val). Walk pages until pages yield 0 cards.
"""
import csv
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

SOURCE_ID = "nia_most_wanted"
SOURCE_AGENCY = "National Investigation Agency (NIA)"
SOURCE_LIST = "Most Wanted Photos"
BASE = "https://nia.gov.in/most-wanted-photos"
OUT = f"/home/aayush/risk-pipeline/data/{SOURCE_ID}.csv"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Canonical 17-col schema combine.py accepts (source_id is derived at
# load time from the (source_agency, source_list) -> id map in sources.json).
FIELDS = ["source_agency", "source_list", "case_unit", "name",
          "father_name", "date_of_birth", "gender", "address", "reward_amount",
          "details", "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]


def parse_card(card, page_url):
    """label -> value from a div.wanted-modal-card."""
    d = {}
    href = None
    for row in card.select("div.row"):
        lab = row.select_one("div.col-4.lab")
        val = row.select_one("div.col-8.val")
        if not lab or val is None:
            continue
        key = lab.get_text(strip=True).rstrip(":").strip().lower()
        d[key] = val.get_text(" ", strip=True)
        if key == "wanted in":
            a = val.find("a", href=True)
            if a and a["href"].strip():
                href = urljoin(page_url, a["href"].strip())
    img = card.find("img")
    photo = urljoin(page_url, img["src"]) if img and img.get("src") else ""
    return d, href, photo


def scrape():
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA,
                                  viewport={"width": 1920, "height": 1080},
                                  ignore_https_errors=True)
        page = ctx.new_page()
        pg = 0
        empty_streak = 0
        while pg <= 60:
            url = f"{BASE}?page={pg}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                time.sleep(2)
                html = page.content()
            except Exception as e:
                print(f"  page {pg}: ERROR {type(e).__name__} — stop")
                break
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select("div.wanted-modal-card")
            if not cards:
                empty_streak += 1
                print(f"  page {pg}: 0 cards (streak {empty_streak})")
                if empty_streak >= 2:
                    break
                pg += 1
                time.sleep(2)
                continue
            empty_streak = 0
            for card in cards:
                d, href, photo = parse_card(card, url)
                name = d.get("name", "").strip()
                if not name:
                    continue
                case_no = d.get("wanted in", "").strip()
                details = (
                    f"Wanted in: {case_no} | "
                    f"Organization: {d.get('organization','').strip()} | "
                    f"Status: {d.get('accused status','').strip()} | "
                    f"Aliases: {d.get('aliases','').strip()} | "
                    f"Age/DOB: {d.get('age/dob (approx)','').strip()} | "
                    f"Phone: {d.get('phone no.','').strip()} | "
                    f"Email: {d.get('e-mail','').strip()} | "
                    f"Postal: {d.get('postal address','').strip()}"
                )
                rows.append({
                    "source_id": SOURCE_ID, "source_agency": SOURCE_AGENCY,
                    "source_list": SOURCE_LIST,
                    "case_unit": case_no,
                    "name": name,
                    "father_name": d.get("parentage", "").strip(),
                    "date_of_birth": d.get("age/dob (approx)", "").strip(),
                    "gender": "",
                    "address": d.get("address", "").strip()
                               or d.get("postal address", "").strip(),
                    "reward_amount": d.get("reward", "").strip(),
                    "details": details,
                    "has_document": "Yes" if photo else "No",
                    "document_url": photo,
                    "detail_page_url": href or url,
                    "interpol_notice_id": "", "link_kind": "",
                    "scraped_at": now, "enrichment_status": "",
                })
            print(f"  page {pg}: {len(cards)} cards (total {len(rows)})")
            pg += 1
            time.sleep(2)
        browser.close()

    # de-dupe on (name, case_unit)
    seen, uniq = set(), []
    for r in rows:
        k = (r["name"].lower(), r["case_unit"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(uniq)
    empties = sum(1 for r in uniq if not r["name"])
    print(f"\n{SOURCE_ID}: {len(uniq)} unique rows -> {OUT} "
          f"(empty names: {empties})")
    for r in uniq[:3]:
        print({k: r[k] for k in ("name", "father_name", "address",
                                 "case_unit", "details")})
    return len(uniq)


if __name__ == "__main__":
    scrape()
