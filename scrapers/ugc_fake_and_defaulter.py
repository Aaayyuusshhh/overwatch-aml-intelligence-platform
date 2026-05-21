"""
UGC University lists:
  - Fake Universities (single table)
  - Defaulter State (Private) Universities (multi-table page; we keep
    only rows tagged as "defaulter" — the page reuses the same shell for
    several categories including the fake list, so we scope by the URL).

Both pages are plain HTML — requests + BeautifulSoup is enough.
"""
import csv, os, re, time
from datetime import datetime, timezone
import requests, urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJECT_ROOT, "data")
LOG = os.path.join(PROJECT_ROOT, "logs", "scrape_session_20260521.log")
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/124",
        "Accept-Language": "en;q=0.9"}
HEADER = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url", "interpol_notice_id",
          "link_kind", "scraped_at", "enrichment_status"]
NOW = datetime.now(timezone.utc).isoformat()


def log(tag, msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} [{tag}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def base(agency, lst, name, **kw):
    r = {h: "" for h in HEADER}
    r.update({"source_agency": agency, "source_list": lst, "name": name, "scraped_at": NOW})
    r.update(kw)
    return r


def write_csv(rows, fname):
    p = os.path.join(DATA, fname)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
    return p, len(rows)


def parse_university_table(table):
    """Yield {sno, state, name_and_address} dicts from a UGC table."""
    rows = []
    headers = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        vals = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in cells]
        if cells[0].name == "th" and not headers:
            headers = vals
            continue
        if len(vals) < 2 or not any(vals):
            continue
        rows.append(vals)
    return headers, rows


def scrape_ugc_fake():
    URL = "https://www.ugc.gov.in/universitydetails/Fakeuniversity"
    r = requests.get(URL, headers=HDRS, timeout=30, verify=False)
    soup = BeautifulSoup(r.text, "lxml")
    tab = soup.find("table")
    if not tab:
        log("ugc_fake", "no table found"); return []
    headers, rows = parse_university_table(tab)
    out = []
    for vals in rows:
        # Columns: [Sr No, State, University Name (with address)]
        state = vals[1] if len(vals) > 1 else ""
        name_full = vals[2] if len(vals) > 2 else vals[-1]
        # Try to split name vs address at the first comma if it has one.
        name, _, addr = name_full.partition(", ")
        out.append(base(
            "University Grants Commission (UGC)", "Fake Universities",
            name.strip()[:300],
            address=addr.strip() if addr else "",
            details=f"state: {state} | sr_no: {vals[0]}",
            detail_page_url=URL,
        ))
    log("ugc_fake", f"{len(out)} fake universities")
    return out


def _col(headers, *aliases):
    """Find a column index whose header contains any of the alias substrings."""
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(a in hl for a in aliases):
            return i
    return -1


def scrape_ugc_violators():
    """The 'HEIs Violating Regulations' page has up to 4 tables:
       0: Fake Universities          -> source_list 'Fake Universities'
       1: Not Following PhD Regs      -> source_list 'HEIs Not Following PhD Regulations'
       2: Without Ombudsperson         -> source_list 'HEIs Without Ombudsperson'
       3: Defaulter State Universities -> source_list 'Defaulter State (Private) Universities'
       Each table has its own column order, so we look up name/state/type
       columns by header label rather than positional index."""
    URL = "https://www.ugc.gov.in/universitydetails/HEIs_Violating_Regulations?tab=Defaulter"
    LIST_MAP = {
        "fake universities": "Fake Universities",
        "phd regulations":    "HEIs Not Following PhD Regulations",
        "ph.d regulations":   "HEIs Not Following PhD Regulations",
        "ph.d. regulations":  "HEIs Not Following PhD Regulations",
        "phd regs":           "HEIs Not Following PhD Regulations",
        "ombudsperson":       "HEIs Without Ombudsperson",
        "defaulter":          "Defaulter State (Private) Universities",
    }
    r = requests.get(URL, headers=HDRS, timeout=30, verify=False)
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    # First-pass already covered by scrape_ugc_fake on the dedicated URL;
    # here we capture the THREE additional categories that only live here.
    for tab in soup.find_all("table"):
        heading_el = tab.find_previous(["h1", "h2", "h3", "h4"])
        if not heading_el:
            continue
        heading = heading_el.get_text(" ", strip=True).lower()
        list_name = None
        for kw, ln in LIST_MAP.items():
            if kw in heading:
                list_name = ln; break
        if not list_name or list_name == "Fake Universities":
            # skip the fake-uni dupe (covered by scrape_ugc_fake)
            continue
        headers, rows = parse_university_table(tab)
        if not headers:
            continue
        i_name  = _col(headers, "university name", "name")
        i_state = _col(headers, "state")
        i_type  = _col(headers, "type")
        i_addr  = _col(headers, "address")
        if i_name < 0:
            continue
        for vals in rows:
            if i_name >= len(vals): continue
            name = vals[i_name]
            state = vals[i_state] if 0 <= i_state < len(vals) else ""
            typ   = vals[i_type]  if 0 <= i_type  < len(vals) else ""
            addr  = vals[i_addr]  if 0 <= i_addr  < len(vals) else ""
            # name may be "X, address text..." when there's no address column
            if not addr and "," in name and i_addr < 0:
                name, _, addr_inline = name.partition(", ")
                addr = addr_inline
            if not name or len(name.strip()) < 3:
                continue
            detail = " | ".join(filter(None, [
                f"state: {state}" if state else None,
                f"type: {typ}" if typ else None,
                f"sr_no: {vals[0]}" if vals and vals[0] else None,
            ]))
            out.append(base(
                "University Grants Commission (UGC)", list_name,
                name.strip()[:300], address=addr.strip(),
                details=detail, detail_page_url=URL,
            ))
    log("ugc_violators", f"{len(out)} rows across PhD/Ombudsperson/Defaulter lists")
    return out


def main():
    p, n = write_csv(scrape_ugc_fake(), "ugc_fake_universities.csv"); log("write", f"{n} -> {p}")
    violators = scrape_ugc_violators()
    # Split by list_name into separate CSVs for clean source_id mapping.
    by_list = {}
    for r in violators:
        by_list.setdefault(r["source_list"], []).append(r)
    name_to_file = {
        "HEIs Not Following PhD Regulations": "ugc_heis_phd_regs_violators.csv",
        "HEIs Without Ombudsperson":          "ugc_heis_without_ombudsperson.csv",
        "Defaulter State (Private) Universities": "ugc_defaulter_state_private_universities.csv",
    }
    for list_name, rows in by_list.items():
        fname = name_to_file.get(list_name)
        if not fname: continue
        p, n = write_csv(rows, fname); log("write", f"{n} -> {p}")


if __name__ == "__main__":
    main()
