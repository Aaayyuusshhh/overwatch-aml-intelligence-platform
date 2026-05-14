"""
FCRA Registered Associations scraper (#71).

Source: https://fcraonline.nic.in/fc_deemed_asso_list.aspx

This is the MHA FCRA online portal. The page is a stateless ASP.NET
form: it has no __VIEWSTATE in static HTML and no public JSON API.
The data table only populates after the user selects a state from
'ddlstate', selects rdlist=2 (All), and clicks the Submit button -
the resulting POST returns ~10-300 KB of HTML containing the rows
for that state. The session cookie issued on first GET is required
for the POST to be honoured (Scrapling-only POST hits the error
page).

Approach: drive the form with Playwright (browser session keeps the
cookies). Iterate every state code, pull the populated table, accumulate.

Columns from the FCRA table:
  S.No | Registration | AssociationName | Address | Nature

Mapped to the 17-column shared schema:
  case_unit   = Registration number
  name        = AssociationName
  address     = Address
  details     = "Nature: <comma-separated work areas>"
  detail_page_url = state-scoped landing URL
  link_kind   = 'fcra_registered'

Sanity floor: 5,000 rows (the list has been ~14k+ historically; a
sharp drop signals MHA changed the form or our state codes).
"""

import csv
import os
import re
import sys
from datetime import datetime

# Guarded Playwright import.
try:
    from playwright.sync_api import sync_playwright
    _PW_AVAILABLE = True
except Exception as _e:                      # pragma: no cover
    _PW_AVAILABLE = False
    _PW_ERR = f"{type(_e).__name__}: {_e}"

LIST_URL = "https://fcraonline.nic.in/fc_deemed_asso_list.aspx"
EXPECTED_MIN = 5_000

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "fcra_registered_associations.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# State codes -> display name (matches the ddlstate <option value=>).
STATES = {
    "01": "Andhra Pradesh",
    "02": "Assam",
    "03": "Bihar",
    "29": "Chandigarh",
    "32": "Chhattisgarh",
    "07": "Delhi",
    "33": "Goa",
    "04": "Gujarat",
    "05": "Haryana",
    "06": "Himachal Pradesh",
    "08": "Jammu & Kashmir",
    "34": "Jharkhand",
    "09": "Karnataka",
    "10": "Kerala",
    "11": "Madhya Pradesh",
    "12": "Maharashtra",
    "13": "Manipur",
    "14": "Meghalaya",
    "15": "Mizoram",
    "16": "Nagaland",
    "17": "Odisha",
    "20": "Punjab",
    "21": "Rajasthan",
    "22": "Sikkim",
    "23": "Tamil Nadu",
    "30": "Tripura",
    "37": "Telangana",
    "27": "Uttar Pradesh",
    "36": "Uttarakhand",
    "28": "West Bengal",
}


def _clean(s):
    if s is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", s).replace("&nbsp;", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_largest_table(html):
    """Return (header_cells, [row_cells]) for the table with the most <tr>s."""
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", html, re.I)
    best = []
    for t in tables:
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
        if len(trs) > len(best):
            best = trs
    if not best:
        return None, []
    rows = []
    for tr in best:
        cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)
        rows.append([_clean(c) for c in cells])
    if not rows:
        return None, []
    return rows[0], rows[1:]


def scrape():
    if not _PW_AVAILABLE:
        raise RuntimeError(f"Playwright not available: {_PW_ERR}")

    seen_keys = set()
    out_records = []

    # Reuse a saved browser profile when available so the FCRA ASP.NET
    # session cookies don't restart from zero on every run. Saved at the
    # end of a successful sweep.
    from utils.browser_profiles import load_profile, save_profile

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = load_profile(browser, "fcra")
            page = context.new_page()
            page.goto(LIST_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(2_000)

            scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            for code, state_name in STATES.items():
                try:
                    page.select_option("select[name=ddlstate]", value=code, timeout=20_000)
                    page.wait_for_timeout(800)
                    page.check("input[name=rdlist][value='2']")
                    page.wait_for_timeout(300)
                    page.click("input[name=btsubmitt]")
                    page.wait_for_load_state("networkidle", timeout=45_000)
                    page.wait_for_timeout(800)
                    html = page.content()
                    headers, rows = _parse_largest_table(html)
                    new = 0
                    for cells in rows:
                        if not any(c.strip() for c in cells):
                            continue
                        # Expect: S.No, Registration, AssociationName, Address, Nature
                        if len(cells) < 4:
                            continue
                        # Skip header-row repeats.
                        joined = "|".join(cells).lower()
                        if "associationname" in joined.replace(" ", "") or \
                           "registration" in joined and "address" in joined and "nature" in joined:
                            continue
                        # Build a unique key (registration number is most unique;
                        # fall back to AssociationName + state).
                        regn = cells[1] if len(cells) > 1 else ""
                        name = cells[2] if len(cells) > 2 else ""
                        key = (regn, name) if regn else (state_name, name)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        addr = cells[3] if len(cells) > 3 else ""
                        nature = cells[4] if len(cells) > 4 else ""
                        out_records.append({
                            "source_agency": "MHA",
                            "source_list": "FCRA Registered Associations",
                            "case_unit": regn,
                            "name": name,
                            "father_name": "",
                            "date_of_birth": "",
                            "gender": "",
                            "address": addr,
                            "reward_amount": "",
                            "details": (f"Nature: {nature} | State: {state_name}"
                                        if nature else f"State: {state_name}"),
                            "has_document": "No",
                            "document_url": "",
                            "detail_page_url": LIST_URL,
                            "interpol_notice_id": "",
                            "link_kind": "fcra_registered",
                            "scraped_at": scraped_at,
                            "enrichment_status": "none",
                        })
                        new += 1
                    print(f"  state {code} {state_name:25s}  rows={len(rows):>5}  new={new}")
                    # Go back to clear the form for the next state.
                    page.go_back(wait_until="networkidle", timeout=30_000)
                    page.wait_for_timeout(800)
                except Exception as e:
                    print(f"  state {code} {state_name}: {type(e).__name__}: {str(e)[:120]}")
                    # Recover by reloading the form page.
                    try:
                        page.goto(LIST_URL, wait_until="networkidle", timeout=30_000)
                        page.wait_for_timeout(1_500)
                    except Exception:
                        pass
            # Persist session for next run before tearing down the browser.
            save_profile(context, "fcra")
        finally:
            browser.close()

    if len(out_records) < EXPECTED_MIN:
        raise RuntimeError(
            f"FCRA: extracted {len(out_records)} rows, below floor {EXPECTED_MIN} "
            "(MHA may have changed the form layout or state codes)"
        )
    return out_records


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
    print("FCRA Registered Associations scraper (#71) - Playwright")
    print("=" * 60)
    records = scrape()
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
