#!/usr/bin/env python3
"""Friday 2026-05-22 — US + Australia + New Zealand enforcement scrapers.

Targets (status from recon):
  * NY DFS Enforcement Actions   — HTML table          (replaces global_us_nydfs_press)
  * APRA Disqualified Individuals — HTML tables (x3)   NEW
  * FMA Enforcement Cases         — HTML article list  (updates global_nz_fma_enforcement)
  * FMA Warnings and Alerts       — HTML article list  NEW
  * CA DFPI Press Releases        — WP REST API        NEW
  * CA DFPI Consumer Alerts       — WP REST API        NEW

Skipped (probe results):
  * TX SOS         — no public enforcement list
  * FL OFR         — ConnectTimeout
  * AUSTRAC        — ReadTimeout
  * RBNZ           — 403 Cloudflare block
  * ASIC banned    — search-only, no public list
  * ASIC media     — JS-rendered (already handled by global_au_asic_recent_media_releases)
"""
import csv
import json
import os
import re
import sys
import time
import warnings
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

DATA_DIR = "/home/aayush/risk-pipeline/data"
H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
FIELDS = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _mkrow(agency, lst, **kw):
    r = {f: "" for f in FIELDS}
    r["source_agency"] = agency
    r["source_list"] = lst
    r["scraped_at"] = _now()
    r.update({k: v for k, v in kw.items() if k in FIELDS})
    return r


def _write_csv(sid, rows):
    path = os.path.join(DATA_DIR, f"{sid}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


# ---------- NY DFS Enforcement Actions ----------
def scrape_nydfs():
    SID = "global_us_nydfs_press"
    AG, LST = "NY-DFS", "Press"
    URL = "https://www.dfs.ny.gov/industry_guidance/enforcement_actions"
    print(f"\n[{SID}] {URL}")
    r = requests.get(URL, headers=H, timeout=30, verify=False)
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        print(f"  {SID}: no table found")
        return SID, []
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 3 or cells[0].lower() == "action date":
            continue
        date, action, category = cells[0], cells[1], cells[2]
        # find pdf link in this row
        pdf = ""
        link = tr.find("a", href=True)
        if link:
            pdf = urljoin(URL, link["href"])
        rows.append(_mkrow(
            AG, LST,
            name=action,
            details=f"Category: {category} | Date: {date}",
            has_document="Yes" if pdf else "No",
            document_url=pdf,
            detail_page_url=URL,
            link_kind="enforcement_action",
        ))
    path = _write_csv(SID, rows)
    print(f"  {SID}: {len(rows)} rows -> {path}")
    return SID, rows


# ---------- APRA Disqualified Individuals ----------
def scrape_apra_disqualified():
    SID = "au_apra_disqualified_individuals"
    AG, LST = "APRA", "Disqualified Individuals"
    URL = "https://www.apra.gov.au/disqualified-individuals"
    print(f"\n[{SID}] {URL}")
    r = requests.get(URL, headers=H, timeout=30, verify=False)
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    for h_tag in soup.find_all(["h2", "h3"]):
        title = h_tag.get_text(" ", strip=True)
        table = h_tag.find_next("table")
        if not table:
            continue
        # which list is this?
        if "revok" in title.lower():
            list_kind = "revoked"
        elif "far" in title.lower():
            list_kind = "FAR"
        else:
            list_kind = "general"
        headers = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not headers and tr.find("th"):
                headers = [h.lower() for h in cells]
                continue
            if len(cells) < 2:
                continue
            # build dict
            m = dict(zip(headers, cells)) if headers else {}
            last = m.get("last name", "") or (cells[0] if cells else "")
            given = m.get("given names", "") or (cells[1] if len(cells) > 1 else "")
            name = f"{given} {last}".strip()
            if not name or name.lower() in ("name", ""):
                continue
            scope = m.get("scope of disqualification (s. 42(2))", "") or m.get("scope of disqualification", "")
            entity = m.get("accountable entity", "") or m.get("entity", "")
            sig = m.get("significant related entity", "")
            eff = m.get("effective date", "") or m.get("date", "")
            end = m.get("end date", "")
            details_parts = []
            if scope: details_parts.append(f"Scope: {scope}")
            if entity: details_parts.append(f"Entity: {entity}")
            if sig: details_parts.append(f"Related: {sig}")
            if eff: details_parts.append(f"Effective: {eff}")
            if end: details_parts.append(f"End: {end}")
            details_parts.append(f"Kind: {list_kind}")
            rows.append(_mkrow(
                AG, LST,
                name=name,
                details=" | ".join(details_parts),
                detail_page_url=URL,
                link_kind="disqualification",
            ))
    path = _write_csv(SID, rows)
    print(f"  {SID}: {len(rows)} rows -> {path}")
    return SID, rows


# ---------- FMA Enforcement Cases ----------
def scrape_fma_enforcement():
    SID = "global_nz_fma_enforcement"
    AG, LST = "FMA", "Enforcement"
    URL = "https://www.fma.govt.nz/about-us/enforcement/cases/"
    print(f"\n[{SID}] {URL}")
    r = requests.get(URL, headers=H, timeout=30, verify=False)
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    # Articles, each with an h3 case title
    seen = set()
    for art in soup.find_all("article"):
        h = art.find(["h2", "h3"])
        if not h:
            continue
        name = h.get_text(" ", strip=True)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        # Look for date, status, descriptive text
        text_parts = []
        for p in art.find_all(["p", "div"], limit=3):
            t = p.get_text(" ", strip=True)
            if t and 8 < len(t) < 400:
                text_parts.append(t)
        link = art.find("a", href=True)
        case_url = urljoin(URL, link["href"]) if link else URL
        rows.append(_mkrow(
            AG, LST,
            name=name,
            details=" | ".join(text_parts[:2])[:500],
            detail_page_url=case_url,
            link_kind="enforcement_case",
        ))
    # Also catch h3 cases not in <article>
    for h in soup.find_all("h3"):
        name = h.get_text(" ", strip=True)
        if not name or name.lower() in seen or len(name) < 3:
            continue
        if name.lower() in ("looking for information?", "enforcement activity"):
            continue
        # skip navigation
        if h.find_parent("nav"):
            continue
        seen.add(name.lower())
        rows.append(_mkrow(
            AG, LST,
            name=name,
            details="FMA enforcement case",
            detail_page_url=URL,
            link_kind="enforcement_case",
        ))
    path = _write_csv(SID, rows)
    print(f"  {SID}: {len(rows)} rows -> {path}")
    return SID, rows


# ---------- FMA Warnings and Alerts ----------
def scrape_fma_warnings():
    SID = "nz_fma_warnings_alerts"
    AG, LST = "FMA", "Warnings and Alerts"
    URL = "https://www.fma.govt.nz/library/warnings-and-alerts/"
    print(f"\n[{SID}] {URL}")
    r = requests.get(URL, headers=H, timeout=30, verify=False)
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    seen = set()
    for art in soup.find_all("article"):
        h = art.find(["h2", "h3"])
        if not h:
            continue
        name = h.get_text(" ", strip=True)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        text_parts = []
        for p in art.find_all(["p", "div"], limit=3):
            t = p.get_text(" ", strip=True)
            if t and 8 < len(t) < 400:
                text_parts.append(t)
        link = art.find("a", href=True)
        case_url = urljoin(URL, link["href"]) if link else URL
        rows.append(_mkrow(
            AG, LST,
            name=name,
            details=" | ".join(text_parts[:2])[:500],
            detail_page_url=case_url,
            link_kind="warning_alert",
        ))
    path = _write_csv(SID, rows)
    print(f"  {SID}: {len(rows)} rows -> {path}")
    return SID, rows


# ---------- CA DFPI Press Releases (WP REST API) ----------
def _fetch_wp(endpoint, max_pages=20):
    items = []
    page = 1
    while page <= max_pages:
        url = f"https://dfpi.ca.gov/wp-json/wp/v2/{endpoint}?per_page=100&page={page}"
        try:
            r = requests.get(url, headers=H, timeout=30, verify=False)
            if r.status_code != 200:
                break
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"  WP page {page} error: {type(e).__name__}; stopping")
            break
    return items


def scrape_dfpi_press_releases():
    SID = "us_state_ca_dfpi_press_releases"
    AG, LST = "DFPI", "Press Releases"
    URL = "https://dfpi.ca.gov/news/press-releases/"
    print(f"\n[{SID}] WP API press_release")
    items = _fetch_wp("press_release")
    rows = []
    for it in items:
        title = (it.get("title") or {}).get("rendered", "")
        title = BeautifulSoup(title, "html.parser").get_text(" ", strip=True)
        link = it.get("link", "")
        date = it.get("date", "")
        excerpt_raw = (it.get("excerpt") or {}).get("rendered", "")
        excerpt = BeautifulSoup(excerpt_raw, "html.parser").get_text(" ", strip=True)[:400]
        if not title:
            continue
        rows.append(_mkrow(
            AG, LST,
            name=title,
            details=f"Date: {date} | {excerpt}",
            detail_page_url=link or URL,
            link_kind="press_release",
        ))
    path = _write_csv(SID, rows)
    print(f"  {SID}: {len(rows)} rows -> {path}")
    return SID, rows


def scrape_dfpi_alerts():
    SID = "us_state_ca_dfpi_consumer_alerts"
    AG, LST = "DFPI", "Consumer Alerts"
    URL = "https://dfpi.ca.gov/consumer-alerts/"
    print(f"\n[{SID}] WP API alert")
    items = _fetch_wp("alert")
    rows = []
    for it in items:
        title = (it.get("title") or {}).get("rendered", "")
        title = BeautifulSoup(title, "html.parser").get_text(" ", strip=True)
        link = it.get("link", "")
        date = it.get("date", "")
        excerpt_raw = (it.get("excerpt") or {}).get("rendered", "")
        excerpt = BeautifulSoup(excerpt_raw, "html.parser").get_text(" ", strip=True)[:400]
        if not title:
            continue
        rows.append(_mkrow(
            AG, LST,
            name=title,
            details=f"Date: {date} | {excerpt}",
            detail_page_url=link or URL,
            link_kind="consumer_alert",
        ))
    path = _write_csv(SID, rows)
    print(f"  {SID}: {len(rows)} rows -> {path}")
    return SID, rows


# ---------- runner ----------
SCRAPERS = [
    scrape_nydfs,
    scrape_apra_disqualified,
    scrape_fma_enforcement,
    scrape_fma_warnings,
    scrape_dfpi_press_releases,
    scrape_dfpi_alerts,
]


def main():
    results = []
    for fn in SCRAPERS:
        t0 = time.time()
        try:
            sid, rows = fn()
            results.append((sid, len(rows), time.time() - t0, "ok"))
        except Exception as e:
            print(f"  {fn.__name__} FAILED: {type(e).__name__}: {e}")
            results.append((fn.__name__, 0, time.time() - t0, f"error: {type(e).__name__}"))
    print("\n=== Scrape summary ===")
    print(f"{'SOURCE_ID':<45} {'ROWS':<8} {'TIME':<8} STATUS")
    for sid, n, t, st in results:
        print(f"{sid:<45} {n:<8} {t:<8.1f} {st}")
    return results


if __name__ == "__main__":
    main()
