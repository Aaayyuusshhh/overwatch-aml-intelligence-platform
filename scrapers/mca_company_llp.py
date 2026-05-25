#!/usr/bin/env python3
"""MCA Company/LLP "Under Alert" scraper — defaulter companies, defaulter directors,
dormant companies (DIN-range director lists).

Uses the MCA AEM dmslist API (/bin/dms/searchDocList) by folder ID.
Folder map (from the page DOM data-dialog attribute):
  382 = Defaulter Companies (15 PDFs, by ROC + by name-range)
  383 = Defaulter Directors (15 PDFs, by ROC + by DIN-range)
  384 = Dormant Companies   (14 PDFs, by DIN-range)

The MCA portal returns 0 bytes for ~half the PDFs (broken server links). The
remainder include text-extractable PDFs that we parse here.

Defaulter-company line format:
  [SrNo]  [CIN]  [Company Name]
e.g. "1 U72300DL2007PTC167900 S R MYTAXCARE PRIVATE LIMITED"

Defaulter/Dormant director line format:
  [DIN]  [Director Name…]  [CIN]  [Company Name…]  [Defaulting Year]
e.g. "00001063 AVISHA GOPALKRISHNAN U91990MH2005NPL151336 DESH SEVA SAMITI 2006-07"
"""
from __future__ import annotations
import argparse, csv, os, re, sys, time, warnings, urllib3, hashlib
from datetime import datetime, timezone
import requests
import pdfplumber

# Reuse helpers from existing scraper
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mca_rd_roc import (
    get_session, list_docs, download_pdf, extract_text_all_pages,
    FIELDS, AGENCY, CIN_RE, H_BROWSER,
)

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
PDF_CACHE = os.path.join(_PROJECT_ROOT, "data", "mca_pdf_cache")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PDF_CACHE, exist_ok=True)

# DIN format on this portal: 8-digit zero-padded number (e.g. 00001063)
DIN8_RE = re.compile(r"\b(\d{8})\b")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# (source_id, list_name, folder_id, page_url, parser_kind)
SOURCES = [
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
     "directors"),
]


# ---------------------------------------------------------------------------
# Parser: defaulting companies (CIN + company name on each line)
# ---------------------------------------------------------------------------
def parse_companies(text_pages, doc_meta):
    rows = []
    pdf_title = doc_meta.get("column1", "")
    pdf_date = doc_meta.get("column3", "")
    HDR_NOISE = {"cin", "company", "name", "list", "of", "defaulting",
                 "companies", "for", "the", "year", "office", "registrar",
                 "s.no", "sno", "dated", "no", "S.No.", "annual", "return",
                 "balance", "sheet", "fy"}
    for page_num, text in text_pages:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        for i, line in enumerate(lines):
            cin_match = CIN_RE.search(line)
            if not cin_match:
                continue
            cin = cin_match.group(0)
            after = line[cin_match.end():].strip()
            # Company name is everything after CIN. Strip leading punctuation.
            after = re.sub(r"^[\s|,.\-:;]+", "", after)
            company = after
            # Often a long company name continues on the next line; join if
            # the next line has no CIN and looks like a continuation.
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                if not CIN_RE.search(nxt) and not re.match(r"^\d+\s", nxt):
                    # heuristic: continuation if line is < 60 chars or ends abruptly
                    if (company and not re.search(r"(LIMITED|LTD\.?|LLP|PRIVATE|CO\.?)$",
                                                   company, flags=re.IGNORECASE)
                            and len(nxt) < 80):
                        company = (company + " " + nxt).strip()
            if not company:
                # Try before CIN
                before = line[:cin_match.start()].strip()
                before = re.sub(r"^\d+[\s.):-]*", "", before).strip()
                if not before or before.lower() in HDR_NOISE:
                    continue
                company = before
            company = re.sub(r"\s+", " ", company)[:200]
            if not company or len(company) < 3 or company.lower() in HDR_NOISE:
                continue
            details = (f"CIN: {cin} | Source PDF: {pdf_title[:100]}"
                       f" | PDF date: {pdf_date}"
                       f" | Type: Defaulter (annual filing default)")
            rows.append({"name": company, "details": details, "cin": cin})
    return rows


# ---------------------------------------------------------------------------
# Parser: directors lists (DIN + Name + CIN + Company + Year)
# ---------------------------------------------------------------------------
def parse_directors(text_pages, doc_meta):
    rows = []
    pdf_title = doc_meta.get("column1", "")
    pdf_date = doc_meta.get("column3", "")
    NOISE = {"signatory", "id", "name", "cin", "company", "defaulting", "year",
             "dated", "list", "of", "defaulter", "directors", "no", "sno"}
    for page_num, text in text_pages:
        lines = [ln for ln in text.split("\n") if ln.strip()]
        # Pre-pass: group lines into "rows" by DIN-at-start.
        groups = []  # each is a single combined line
        cur = None
        for ln in lines:
            ln_s = ln.strip()
            # Skip header lines
            tokens_lower = ln_s.lower().split()
            if all(t.strip(":,.") in NOISE for t in tokens_lower):
                continue
            if re.match(r"^\s*\d{8}\b", ln):
                if cur is not None:
                    groups.append(cur)
                cur = ln_s
            else:
                if cur is None:
                    continue  # text before first DIN row (header, etc.)
                cur += " " + ln_s
        if cur is not None:
            groups.append(cur)
        # Now parse each combined row
        for g in groups:
            din_m = re.match(r"^\s*(\d{8})\s+", g)
            cin_m = CIN_RE.search(g)
            if not din_m or not cin_m:
                continue
            din = din_m.group(1)
            cin = cin_m.group(0)
            # Name = text between DIN and CIN
            name = g[din_m.end():cin_m.start()].strip()
            name = re.sub(r"\s+", " ", name).strip()
            # Company = text from end-of-CIN up to the trailing year(s)
            after_cin = g[cin_m.end():].strip()
            # Detect first occurrence of a year-range like "2006-07" or "2006-2007"
            year_m = re.search(r"\b(?:19|20)\d{2}\s*[-–]\s*(?:\d{2,4})", after_cin)
            if year_m:
                company = after_cin[:year_m.start()].strip()
                year = after_cin[year_m.start():].strip()
            else:
                company = after_cin.strip()
                year = ""
            company = re.sub(r"\s+", " ", company).strip()
            # Sanity
            if not name or len(name) < 2 or name.lower() in NOISE:
                continue
            if not company:
                company = "(unknown company)"
            details = (f"DIN: {din} | Company: {company[:120]} | CIN: {cin}"
                       f" | Defaulting Year(s): {year[:60]}"
                       f" | Source PDF: {pdf_title[:80]} | PDF date: {pdf_date}")
            rows.append({"name": name[:200], "details": details, "din": din, "cin": cin})
    return rows


PARSERS = {
    "companies": parse_companies,
    "directors": parse_directors,
}


def run_source(sid, lst, folder, page_url, parser_kind, session, max_pages):
    print(f"\n=== {sid}  folder={folder}  parser={parser_kind} ===")
    docs, total = list_docs(session, folder, page_url)
    print(f"  found {total} docs (got {len(docs)})")
    if not docs:
        return 0, 0, 0
    now = datetime.now(timezone.utc).isoformat()
    parser = PARSERS[parser_kind]
    out_path = os.path.join(DATA_DIR, f"{sid}.csv")
    total_rows = 0
    n_text = 0
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
            cache_name = hashlib.md5(doc_id.encode()).hexdigest()[:16] + ".pdf"
            pdf_path = os.path.join(PDF_CACHE, cache_name)
            if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
                content = download_pdf(session, doc_id, page_url)
                if not content:
                    n_broken += 1
                    print(f"  [{i:2d}/{len(docs)}] {title:55s}  BROKEN (server returned 0 bytes)")
                    continue
                with open(pdf_path, "wb") as pf:
                    pf.write(content)
            text_pages = extract_text_all_pages(pdf_path)
            if not text_pages:
                n_empty += 1
                print(f"  [{i:2d}/{len(docs)}] {title:55s}  scanned-image (0 pages with text)")
                continue
            if max_pages:
                text_pages = text_pages[:max_pages]
            n_text += 1
            parsed = parser(text_pages, doc)
            new_rows = 0
            for r in parsed:
                # Dedup key — use CIN+name for companies, DIN+CIN+name for directors
                if parser_kind == "companies":
                    key = (r.get("cin", ""), r["name"].lower())
                else:
                    key = (r.get("din", ""), r.get("cin", ""), r["name"].lower())
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
            print(f"  [{i:2d}/{len(docs)}] {title:55s}  pages={len(text_pages):3d}  +{new_rows} (total={total_rows})")
            time.sleep(0.3)
    print(f"  DONE: {sid} -> {out_path}")
    print(f"    rows={total_rows}, pdfs_text={n_text}, pdfs_empty={n_empty}, broken={n_broken}")
    return total_rows, len(docs), n_text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=0,
                    help="cap pages per PDF parsed (0 = all)")
    ap.add_argument("--only", help="run only one source_id")
    args = ap.parse_args()

    session = get_session()
    summary = []
    for sid, lst, folder, page_url, parser_kind in SOURCES:
        if args.only and sid != args.only:
            continue
        try:
            n_rows, n_docs, n_text = run_source(
                sid, lst, folder, page_url, parser_kind, session,
                args.max_pages or None)
            summary.append((sid, n_docs, n_text, n_rows))
        except Exception as e:
            print(f"  EXCEPTION in {sid}: {type(e).__name__}: {e}")
            summary.append((sid, 0, 0, 0))
    print("\n=== SUMMARY ===")
    for sid, n_docs, n_text, n_rows in summary:
        print(f"  {sid:40s}  pdfs={n_docs:>3d}  parseable={n_text:>3d}  rows={n_rows:>7d}")


if __name__ == "__main__":
    main()
