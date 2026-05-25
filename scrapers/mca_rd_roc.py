#!/usr/bin/env python3
"""MCA RD/ROC scraper — disqualified directors, struck-off companies, proclaimed offenders.

Uses the MCA AEM dmslist API (/bin/dms/searchDocList) to list PDFs by folder,
downloads each, then text-extracts and regex-parses for entity rows.

PDFs vary in quality:
  - many are machine-readable text (Ahmedabad, recent ROCs) -> parseable
  - many are scanned images (older state lists) -> skipped (0 text)
  - some return 0 bytes from the server -> skipped

By default scrapes the most-recent N PDFs per source to keep runtime bounded.
Use --all to process every PDF (can take hours).
"""
from __future__ import annotations
import argparse, csv, json, os, re, sys, time, warnings, urllib3, hashlib
from datetime import datetime, timezone
from urllib.parse import quote
import requests
import pdfplumber

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
PDF_CACHE = os.path.join(_PROJECT_ROOT, "data", "mca_pdf_cache")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PDF_CACHE, exist_ok=True)

FIELDS = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]

AGENCY = "Ministry of Corporate Affairs (MCA)"
H_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="125", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# (source_id, list_name, folder_id, page_url, parser_kind)
SOURCES = [
    ("mca_disqualified_directors_164",
     "Disqualified Directors U/S 164(2)(A)",
     "435",
     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/disqualified-directors.html",
     "directors"),
    ("mca_proclaimed_offenders",
     "Proclaimed Offenders U/S 82 Cr.PC",
     "436",
     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/proclaimed-offenders.html",
     "offenders"),
    ("mca_companies_struck_off",
     "Companies Struck Off (STK-7) U/S 248(5)",
     "433",
     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/companies-struck-roc.html",
     "companies"),
    # Under-alert pages — most PDFs are scanned images, but we register anyway
    ("mca_defaulter_companies",
     "Defaulter Companies (Filing Default)",
     "382",
     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/defaulter-companies.html",
     "companies"),
    ("mca_defaulter_directors",
     "Defaulter Directors (Filing Default)",
     "383",
     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/defaulter-directors.html",
     "directors"),
    ("mca_dormant_companies",
     "Dormant Companies (3yr Filing Default)",
     "384",
     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/dormant-companies.html",
     "companies"),
    ("mca_llps_strike_off",
     "LLPs Under Process of Strike Off",
     "386",
     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/llps-under-strike-off.html",
     "companies"),
    # STK-6 public notices — 2458 PDFs total, recent ones are clean text
    ("mca_public_notices_stk6",
     "Public Notices (STK-6) U/S 248(2)",
     "1443",
     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/public-notices-stk6.html",
     "companies"),
]

# Regexes for the disqualified-directors line format:
# "<SrNo> <CIN-21char> <Company-words...> <DIN-6to8digits> <Director-words...> <RC###> <Status> <date> <date>"
CIN_RE = re.compile(r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}")
DIN_RE = re.compile(r"\b\d{6,8}\b")
ROC_CODE_RE = re.compile(r"\bRC[-A-Z0-9]{2,8}\b")


def get_session():
    s = requests.Session()
    s.get("https://www.mca.gov.in/", headers=H_BROWSER, timeout=30, verify=False)
    return s


def list_docs(session, folder, page_url):
    session.get(page_url, headers=H_BROWSER, timeout=30, verify=False)
    dialog = json.dumps({"folder": str(folder), "language": "English",
                         "totalColumns": 3,
                         "columns": ["Title", "ROC", "Date"]})
    H_api = {**H_BROWSER, "Accept": "application/json, text/javascript, */*; q=0.01",
             "X-Requested-With": "XMLHttpRequest", "Referer": page_url,
             "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
             "Sec-Fetch-Site": "same-origin"}
    params = {"page": 1, "perPage": 1000, "sortField": "Date", "sortOrder": "D",
              "searchField": "", "searchKeyword": "",
              "startDate": "", "endDate": "", "filter": "",
              "dialog": dialog}
    r = session.get("https://www.mca.gov.in/bin/dms/searchDocList",
                    params=params, headers=H_api, timeout=60, verify=False)
    if r.status_code != 200:
        return [], 0
    j = r.json()
    docs = json.loads(j.get("documentDetails", "[]"))
    return docs, int(j.get("totalResults", len(docs)) or 0)


def download_pdf(session, doc_id, page_url):
    """Download one PDF; return bytes (b'' if broken)."""
    url = f"https://www.mca.gov.in/bin/dms/getdocument?mds={doc_id}"
    H_dl = {**H_BROWSER, "Accept": "application/pdf,*/*",
            "Referer": page_url, "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin"}
    try:
        r = session.get(url, headers=H_dl, timeout=120, verify=False, stream=True)
        if r.status_code != 200:
            return b""
        content = r.content
        if not content.startswith(b"%PDF"):
            return b""
        return content
    except Exception:
        return b""


def extract_text_all_pages(pdf_path):
    """Return list of (page_num, text) for every page that has text."""
    out = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                t = page.extract_text() or ""
                if t.strip():
                    out.append((i, t))
    except Exception:
        pass
    return out


def parse_directors_pdf(text_pages, doc_meta):
    """Parse disqualified-directors text. Two layouts observed:
       Ahmedabad/Mumbai: <SrNo> <CIN> <Company> <DIN> <Director> <ROC> <Status>
       Delhi/Haryana:    <SrNo> <DIN> <Director> <Company> <CIN> <ROC> <Status>
    Detect order from CIN vs DIN positions on each line; slice accordingly."""
    rows = []
    pdf_title = doc_meta.get("column1", "")
    roc = doc_meta.get("column2", "")
    date = doc_meta.get("column3", "")
    # column-header words that show up as noise on each page (skip lines that
    # are just those words)
    NOISE = {"violated", "section", "list", "office", "company", "status",
             "reason", "for", "as", "on", "disqualificat", "code", "period"}
    for page_num, text in text_pages:
        # Join continuation lines that don't start with serial number
        lines_in = text.split("\n")
        joined = []
        for ln in lines_in:
            ln = ln.strip()
            if not ln:
                continue
            tokens = ln.lower().split()
            if all(t in NOISE for t in tokens):
                continue
            if joined and not re.match(r"^\d+\s", ln) and not CIN_RE.search(ln):
                joined[-1] = joined[-1] + " " + ln
            else:
                joined.append(ln)
        for line in joined:
            cin_match = CIN_RE.search(line)
            din_match = DIN_RE.search(line)
            if not cin_match or not din_match:
                continue
            rc_match = ROC_CODE_RE.search(line)
            cin = cin_match.group(0)
            din = din_match.group(0)
            roc_code = rc_match.group(0) if rc_match else ""
            if cin_match.start() < din_match.start():
                # Ahmedabad: <CIN> <Company> <DIN> <Director> <RC>
                company = line[cin_match.end():din_match.start()].strip()
                director_end = rc_match.start() if rc_match else len(line)
                director = line[din_match.end():director_end].strip()
            else:
                # Delhi: <DIN> <Director> <Company> <CIN> <RC>
                # everything between DIN and CIN, then we need to split director
                # from company. Heuristic: company name has an "all caps" run of
                # 2+ words ending in LIMITED/LTD/PRIVATE; everything before that
                # is the director.
                mid = line[din_match.end():cin_match.start()].strip()
                # Find the last "LIMITED|LTD|LLP" token's position
                co_match = re.search(
                    r"\b[A-Z][A-Z0-9&.,()\- /]{2,}\s+(?:LIMITED|LTD\.?|LLP|PVT\.?\s*LTD\.?|PRIVATE\s+LIMITED)\b",
                    mid, flags=re.IGNORECASE)
                if co_match:
                    director = mid[:co_match.start()].strip()
                    company = mid[co_match.start():].strip()
                else:
                    # fallback: split first 2-3 words as director, rest as company
                    parts = mid.split()
                    if len(parts) >= 4:
                        director = " ".join(parts[:2])
                        company = " ".join(parts[2:])
                    else:
                        continue
            if not director or len(director) < 3:
                continue
            # Strip leading SrNo if it leaked into director
            director = re.sub(r"^\d+\s+", "", director).strip()
            # Trim sentinel garbage
            director = re.sub(r"\s+(Strike Off|Active|Disqualified|Violated).*$", "",
                              director, flags=re.IGNORECASE).strip()
            if not director or len(director) < 3 or director.lower() in NOISE:
                continue
            details = (f"DIN: {din} | Company: {company[:120]} | CIN: {cin}"
                       f" | ROC: {roc} | ROC Code: {roc_code}"
                       f" | Source PDF: {pdf_title[:80]} | PDF date: {date}")
            rows.append({"name": director, "details": details})
    return rows


def parse_companies_pdf(text_pages, doc_meta):
    """Parse struck-off-companies text. Look for lines with CIN; the words
    just before CIN are the company name."""
    rows = []
    pdf_title = doc_meta.get("column1", "")
    roc = doc_meta.get("column2", "")
    date = doc_meta.get("column3", "")
    for page_num, text in text_pages:
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            cin_match = CIN_RE.search(line)
            if not cin_match:
                continue
            cin = cin_match.group(0)
            # Company name: the chunk just before CIN, or the whole line minus CIN
            before = line[:cin_match.start()].strip()
            after = line[cin_match.end():].strip()
            # Strip leading sno
            before = re.sub(r"^\d+[\s.):-]*", "", before).strip()
            company = before or after or pdf_title[:80]
            # Companies often span: "SrNo  Work-Item  CIN  Company-Name"
            # So after-CIN text may be the real name
            company_after = re.sub(r"^[\s|,.-]+", "", after).strip()
            if company_after and len(company_after) > len(company):
                company = company_after
            company = company.split("  ")[0].strip()
            if not company or len(company) < 3:
                continue
            details = (f"CIN: {cin} | Struck Off PDF: {pdf_title[:80]}"
                       f" | ROC: {roc} | PDF date: {date}"
                       f" | Section: STK-7 U/S 248(5)")
            rows.append({"name": company[:200], "details": details})
    return rows


def parse_offenders_pdf(text_pages, doc_meta):
    """Parse proclaimed-offenders text. Very heterogeneous; capture lines
    that look like "Name <space> S/o ..." or table-like entries."""
    rows = []
    pdf_title = doc_meta.get("column1", "")
    roc = doc_meta.get("column2", "")
    date = doc_meta.get("column3", "")
    for page_num, text in text_pages:
        for line in text.split("\n"):
            line = line.strip()
            if not line or len(line) < 6:
                continue
            # Look for lines with "S/o" or "Sh." pattern, common in PO lists
            m = re.match(r"^(?:\d+[.)]\s*)?(?:Sh\.|Smt\.|Mr\.|Ms\.|Shri|Smt)?\s*"
                         r"([A-Z][A-Za-z. ]{2,60}?)\s+"
                         r"(?:S/o|D/o|W/o|s/o|d/o|w/o|son of|daughter of)",
                         line)
            if m:
                name = m.group(1).strip()
                if len(name) >= 3:
                    rows.append({
                        "name": name[:200],
                        "details": (f"Source: {pdf_title[:80]} | ROC: {roc}"
                                    f" | PDF date: {date} | Raw line: {line[:160]}"),
                    })
    return rows


PARSERS = {
    "directors": parse_directors_pdf,
    "companies": parse_companies_pdf,
    "offenders": parse_offenders_pdf,
}


def run_source(sid, lst, folder, page_url, parser_kind, session, limit_pdfs, max_pages):
    print(f"\n=== {sid}  folder={folder}  parser={parser_kind} ===")
    docs, total = list_docs(session, folder, page_url)
    print(f"  found {total} docs (got {len(docs)})")
    if not docs:
        return 0, 0
    # Prefer larger PDFs (the actual lists, not single-name deletion notices).
    # Sort by size DESC (MB > 200KB > 50KB > <50KB), then date DESC.
    def size_score(d):
        sz = d.get("docSize", "") or ""
        try:
            if "MB" in sz: return 1_000_000 + float(sz.replace("MB","").strip()) * 1000
            if "KB" in sz: return float(sz.replace("KB","").strip())
        except Exception:
            pass
        return 0
    docs = sorted(docs, key=lambda d: -size_score(d))
    if limit_pdfs:
        docs = docs[:limit_pdfs]
        print(f"  capping to {len(docs)} largest PDFs (sorted by size DESC)")
    now = datetime.now(timezone.utc).isoformat()
    parser = PARSERS[parser_kind]
    out_path = os.path.join(DATA_DIR, f"{sid}.csv")
    total_rows = 0
    n_with_text = 0
    n_empty = 0
    n_broken = 0
    seen = set()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for i, doc in enumerate(docs, 1):
            doc_id = doc.get("docID", "")
            title = doc.get("column1", "")[:60]
            doc_url = f"https://www.mca.gov.in/bin/dms/getdocument?mds={doc_id}"
            # Cache by docID hash to allow reruns
            cache_name = hashlib.md5(doc_id.encode()).hexdigest()[:16] + ".pdf"
            pdf_path = os.path.join(PDF_CACHE, cache_name)
            if not os.path.exists(pdf_path):
                content = download_pdf(session, doc_id, page_url)
                if not content:
                    n_broken += 1
                    if i % 20 == 0:
                        print(f"  [{i}/{len(docs)}] {title}  BROKEN")
                    continue
                with open(pdf_path, "wb") as pf:
                    pf.write(content)
            text_pages = extract_text_all_pages(pdf_path)
            if not text_pages:
                n_empty += 1
                if i % 20 == 0:
                    print(f"  [{i}/{len(docs)}] {title}  scanned-image (no text)")
                continue
            if max_pages:
                text_pages = text_pages[:max_pages]
            n_with_text += 1
            parsed = parser(text_pages, doc)
            new_rows = 0
            for r in parsed:
                key = (r["name"].lower(), r.get("details", "")[:50])
                if key in seen:
                    continue
                seen.add(key)
                row_full = {
                    "source_agency": AGENCY, "source_list": lst,
                    "case_unit": "", "name": r["name"],
                    "father_name": "", "date_of_birth": "",
                    "gender": "", "address": "", "reward_amount": "",
                    "details": r["details"], "has_document": "Yes",
                    "document_url": doc_url, "detail_page_url": page_url,
                    "interpol_notice_id": "", "link_kind": "pdf",
                    "scraped_at": now, "enrichment_status": "",
                }
                w.writerow(row_full)
                new_rows += 1
                total_rows += 1
            if i % 5 == 0 or new_rows > 1000:
                print(f"  [{i}/{len(docs)}] {title[:50]:50s}  "
                      f"pages={len(text_pages)}  +{new_rows} rows  (total={total_rows})")
            time.sleep(0.4)
    print(f"  DONE: {sid} -> {out_path}")
    print(f"    rows={total_rows}, pdfs_text={n_with_text}, pdfs_empty(image)={n_empty}, broken={n_broken}")
    return total_rows, len(docs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-pdfs", type=int, default=30,
                    help="cap PDFs per source (0 = all). Default 30")
    ap.add_argument("--max-pages", type=int, default=200,
                    help="cap pages per PDF parsed (0 = all). Default 200")
    ap.add_argument("--only", help="run only one source_id")
    args = ap.parse_args()

    session = get_session()
    summary = []
    for sid, lst, folder, page_url, parser_kind in SOURCES:
        if args.only and sid != args.only:
            continue
        try:
            n_rows, n_docs = run_source(sid, lst, folder, page_url, parser_kind,
                                        session,
                                        args.limit_pdfs or None,
                                        args.max_pages or None)
            summary.append((sid, n_docs, n_rows))
        except Exception as e:
            print(f"  EXCEPTION in {sid}: {type(e).__name__}: {e}")
            summary.append((sid, 0, 0))
    print("\n=== SUMMARY ===")
    for sid, n_docs, n_rows in summary:
        print(f"  {sid:40s}  pdfs_attempted={n_docs:>4d}  rows_extracted={n_rows:>6d}")


if __name__ == "__main__":
    main()
