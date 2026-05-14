"""
NCDEX disciplinary scrapers for #186 (Defaulter Members) and #189
(Members Inspection Penalty List).

Both pages on ncdex.com are JS-rendered Angular shells: static fetch
returns a 4 KB stub. Playwright renders the page and the data tables
appear in the DOM. We extract them and write per-source CSVs.

Two run() entry points:
    run_defaulters() - source_id ncdex_defaulter_members  (#186)
    run_penalties()  - source_id ncdex_inspection_penalty (#189)

Each is wired into sources.json via a separate scraper file value
('ncdex_defaulter_members.py' / 'ncdex_inspection_penalty.py') that
imports from this module - or sources.json points at this file with
two ID-distinct entries that read OUTPUT_FILE_DEFAULTER /
OUTPUT_FILE_PENALTY. To keep things simple, we expose two thin
wrapper scripts; this file holds the shared logic.
"""

import csv
import os
import re
from datetime import datetime

from playwright.sync_api import sync_playwright

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTER_URL = "https://ncdex.com/suspended_member"
PENALTY_URL   = "https://ncdex.com/disclosures/members-inspection-penalty-list"
SURRENDER_URL = "https://ncdex.com/existing-members/surrender-of-membership"
# The Suspended/Expelled/Cessation lists all live on the suspended_member
# page as separate tables identified by their distinctive headers.
EXPELLED_URL  = DEFAULTER_URL
CESSATION_URL = DEFAULTER_URL

DEFAULTER_OUT = os.path.join(PROJECT_ROOT, "data", "ncdex_defaulter_members.csv")
PENALTY_OUT   = os.path.join(PROJECT_ROOT, "data", "ncdex_inspection_penalty.csv")
EXPELLED_OUT  = os.path.join(PROJECT_ROOT, "data", "ncdex_list_of_expelled_members_187.csv")
CESSATION_OUT = os.path.join(PROJECT_ROOT, "data", "ncdex_cessation_of_membership_183.csv")
SURRENDER_OUT = os.path.join(PROJECT_ROOT, "data", "ncdex_list_surrender_member_188.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _clean(s):
    if s is None: return ""
    s = re.sub(r"<[^>]+>", " ", s).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def _render_and_get_tables(url, wait_ms=8000):
    """Return list of (header_cells, data_rows) for every table on the
    rendered page. INCLUDES tables with 0 data rows (header-only) so the
    caller can detect honest 'empty list' state on disciplinary pages."""
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True)
        try:
            ctx = br.new_context(
                ignore_https_errors=True,
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            )
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=60_000)
            pg.wait_for_timeout(wait_ms)
            html = pg.content()
        finally:
            br.close()
    out = []
    for t in re.findall(r"<table[^>]*>([\s\S]*?)</table>", html, re.I):
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
        if len(trs) < 1:
            continue
        rows = []
        for tr in trs:
            cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)
            rows.append([_clean(c) for c in cells])
        if rows:
            out.append((rows[0], rows[1:]))
    return out


def _save_csv(records, out_path, allow_empty=False):
    if not records and not allow_empty:
        raise RuntimeError(f"No records to write to {out_path}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(records)} records to {out_path}")


def _row_to_record(cells, source_list, link_kind, page_url, scraped_at,
                   header_cells):
    """Map a row's cells to the 17-column schema. We try to find the
    'Member Name' / 'Descriptions' column from the header."""
    # Build header_lc -> idx map for column lookup
    hmap = {h.lower(): i for i, h in enumerate(header_cells)}
    name_idx = next((hmap[k] for k in hmap if "member name" in k or "description" in k), 1)
    case_idx = next((hmap[k] for k in hmap if "tmid" in k or "sr." in k), 0)
    date_idx = next((hmap[k] for k in hmap if "date" in k), None)
    file_idx = next((hmap[k] for k in hmap if "file" in k), None)

    name = cells[name_idx] if name_idx < len(cells) else ""
    case = cells[case_idx] if case_idx is not None and case_idx < len(cells) else ""
    when = cells[date_idx] if date_idx is not None and date_idx < len(cells) else ""
    fileurl = ""
    if file_idx is not None and file_idx < len(cells):
        # cells were stripped of HTML; we can re-look at the original
        # via the simple heuristic that File cells are usually empty
        # text but contain a link. Skip resolving file URL for now.
        pass

    if not name:
        return None
    details_parts = []
    for i, val in enumerate(cells):
        if i == name_idx or not val: continue
        h = header_cells[i] if i < len(header_cells) else f"col_{i}"
        details_parts.append(f"{h}: {val}")
    return {
        "source_agency": "NCDEX",
        "source_list": source_list,
        "case_unit": case,
        "name": name,
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": " | ".join(details_parts),
        "has_document": "Yes" if fileurl else "No",
        "document_url": fileurl,
        "detail_page_url": page_url,
        "interpol_notice_id": "",
        "link_kind": link_kind,
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


def _scrape_one(url, source_list, link_kind, header_must_contain,
                allow_empty=False):
    """Render `url`, find the table whose header contains all strings
    in `header_must_contain`, parse its rows. With allow_empty=True we
    return [] when the table is found header-only (honest 'empty list'
    state). Without it we still raise if the table itself is missing."""
    tables = _render_and_get_tables(url)
    print(f"  rendered {url}: {len(tables)} tables")
    target = None
    for headers, rows in tables:
        joined = " ".join(h.lower() for h in headers)
        if all(t.lower() in joined for t in header_must_contain):
            target = (headers, rows)
            print(f"  matched headers={headers} rows={len(rows)}")
            break
    if not target:
        raise RuntimeError(f"NCDEX target table not found "
                           f"(needed headers contain {header_must_contain})")
    headers, rows = target
    if not rows and not allow_empty:
        raise RuntimeError(f"NCDEX target table {header_must_contain} "
                           f"matched but had 0 data rows")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for cells in rows:
        if not any(c for c in cells): continue
        rec = _row_to_record(cells, source_list, link_kind, url, scraped_at, headers)
        if rec: out.append(rec)
    return out


def run_defaulters():
    print("=" * 60)
    print("NCDEX Defaulter Members scraper (#186)")
    print("=" * 60)
    recs = _scrape_one(DEFAULTER_URL,
                       source_list="List of Defaulter Members",
                       link_kind="ncdex_defaulter",
                       header_must_contain=("member name", "date of defaulter"))
    _save_csv(recs, DEFAULTER_OUT)


def run_penalties():
    print("=" * 60)
    print("NCDEX Members Inspection Penalty scraper (#189)")
    print("=" * 60)
    recs = _scrape_one(PENALTY_URL,
                       source_list="Members Inspection Penalty List",
                       link_kind="ncdex_penalty",
                       header_must_contain=("date", "descriptions"))
    _save_csv(recs, PENALTY_OUT)


def run_expelled():
    """#187 List of Expelled Members. The Expelled table sits on
    /suspended_member alongside the Defaulter table. As of build it has
    only a header row — NCDEX has no current expelled members. We honour
    that and write a header-only CSV (empty list, not failure)."""
    print("=" * 60)
    print("NCDEX List of Expelled Members scraper (#187)")
    print("=" * 60)
    recs = _scrape_one(EXPELLED_URL,
                       source_list="List of Expelled Members",
                       link_kind="ncdex_expelled",
                       header_must_contain=("member name", "date of expulsion"),
                       allow_empty=True)
    _save_csv(recs, EXPELLED_OUT, allow_empty=True)


def run_cessation():
    """#183 Cessation of Membership. Sits on /suspended_member with a
    distinct header schema (Particulars/Date of Cessation). Header-only
    as of build."""
    print("=" * 60)
    print("NCDEX Cessation of Membership scraper (#183)")
    print("=" * 60)
    recs = _scrape_one(CESSATION_URL,
                       source_list="Cessation of Membership",
                       link_kind="ncdex_cessation",
                       header_must_contain=("date of cessation",),
                       allow_empty=True)
    _save_csv(recs, CESSATION_OUT, allow_empty=True)


def run_surrender():
    """#188 List Surrender Member. Lives on
    /existing-members/surrender-of-membership as a Date/Descriptions/File
    table of dated surrender notices (PDFs)."""
    print("=" * 60)
    print("NCDEX List Surrender Member scraper (#188)")
    print("=" * 60)
    recs = _scrape_one(SURRENDER_URL,
                       source_list="List Surrender Member",
                       link_kind="ncdex_surrender",
                       header_must_contain=("date", "descriptions"),
                       allow_empty=True)
    _save_csv(recs, SURRENDER_OUT, allow_empty=True)


# Allow `from scrapers.ncdex_disciplinary import run` to work for
# whichever the orchestrator dispatches (two thin wrappers below).
def run():
    run_defaulters()


if __name__ == "__main__":
    run_defaulters()
    run_penalties()
    run_expelled()
    run_cessation()
    run_surrender()
