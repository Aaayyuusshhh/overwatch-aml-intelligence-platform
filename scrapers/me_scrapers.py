"""
Middle East scrapers (UAE + Gulf).

Working:
  - CBUAE Enforcement: text-extractable PDF
  - SCA UAE: parses Open Data page
  - CBB Bahrain: media-center press releases
  - FSA Oman (formerly CMA Oman): probes a couple of paths

Blocked (registered but not scraped):
  - DFSA Regulatory Actions  (Cloudflare 403)
  - DFSA Alerts              (Cloudflare 403)
  - QCB Qatar                (URL redirects to error.aspx)
  - CMA Kuwait               (ConnectTimeout)
"""
import csv, io, os, re, time, requests, urllib3
from datetime import datetime, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import pdfplumber

urllib3.disable_warnings()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJECT_ROOT, "data")
LOG = os.path.join(PROJECT_ROOT, "logs", "scrape_session_20260520.log")
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


# --------------------------- CBUAE Enforcement (PDF) ------------------------
def scrape_cbuae():
    URL = "https://www.centralbank.ae/media/k2appegf/list-of-administrative-and-financial-sanctions-under-the-central-bank-law-and-the-anti-money-laundering-and-combating-financing-of-terrorism-law-en.pdf"
    PAGE = "https://www.centralbank.ae/en/our-operations/enforcement/"
    try:
        r = requests.get(URL, headers=HDRS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log("cbuae", f"PDF download failed: {e}")
        return []
    rows = []
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        for i, page in enumerate(pdf.pages):
            txt = page.extract_text() or ""
            # The CBUAE PDF lists entities and penalty amounts in lines; the
            # actual entity rows usually start with a sequence number.
            for ln in txt.split("\n"):
                ln = ln.strip()
                if not ln or len(ln) < 8:
                    continue
                # entity-like line: starts with index "1 ", "2 " etc., or has currency
                m = re.match(r"^\s*\d+\s+(.+)", ln)
                if not m:
                    continue
                entity = m.group(1).strip()
                # skip obvious headers like "1 List of administrative..."
                if "list of" in entity.lower() or "central bank" in entity.lower()[:30]:
                    continue
                rows.append(base(
                    "Central Bank of the UAE (CBUAE)", "Enforcement Actions",
                    entity[:200],
                    details=f"pdf_page: {i+1} | raw_line: {ln[:400]}",
                    has_document="Yes", document_url=URL, detail_page_url=PAGE,
                ))
    # if the structured parse yields nothing meaningful, register one row with the PDF as document_url
    if not rows:
        rows.append(base(
            "Central Bank of the UAE (CBUAE)", "Enforcement Actions",
            "CBUAE Sanctions List (PDF)",
            details="See attached PDF for current sanctioned entities.",
            has_document="Yes", document_url=URL, detail_page_url=PAGE,
            enrichment_status="pdf_parse_needed",
        ))
    log("cbuae", f"{len(rows)} rows")
    return rows


# --------------------------- SCA UAE Open Data -----------------------------
def scrape_sca_uae():
    URL = "https://www.sca.gov.ae/en/open-data.aspx"
    try:
        r = requests.get(URL, headers=HDRS, timeout=20)
    except Exception as e:
        log("sca_uae", f"fetch failed: {e}")
        return []
    soup = BeautifulSoup(r.text, "lxml")
    rows = []
    # Open Data pages usually have downloadable items - PDFs, CSVs, datasets
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 5:
            continue
        if any(href.lower().endswith(ext) for ext in (".pdf", ".xlsx", ".xls", ".csv", ".json", ".xml")):
            full = urljoin(URL, href)
            rows.append(base(
                "Securities and Commodities Authority (SCA UAE)", "Enforcement Actions",
                text[:200],
                details=f"format: {href.rsplit('.',1)[-1].lower()} | category: open-data",
                has_document="Yes", document_url=full, detail_page_url=URL,
                enrichment_status="document_only",
            ))
    log("sca_uae", f"{len(rows)} open-data document entries")
    return rows


# --------------------------- CBB Bahrain Media -----------------------------
def scrape_cbb_bahrain():
    URL = "https://www.cbb.gov.bh/media-center/"
    try:
        r = requests.get(URL, headers=HDRS, timeout=20)
    except Exception as e:
        log("cbb", f"fetch failed: {e}")
        return []
    soup = BeautifulSoup(r.text, "lxml")
    rows = []
    # Filter to enforcement / penalty / disciplinary related press items
    KW = ("penalty", "fine", "enforcement", "warning", "license", "licence",
          "suspend", "revoked", "directive", "settlement", "violation", "breach",
          "sanction", "compound", "disciplin")
    seen = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 10 or text in seen:
            continue
        low = text.lower()
        if any(k in low for k in KW):
            seen.add(text)
            rows.append(base(
                "Central Bank of Bahrain (CBB)", "Enforcement Actions",
                text[:300],
                details=f"category: press-release",
                detail_page_url=urljoin(URL, a["href"]),
            ))
    log("cbb_bahrain", f"{len(rows)} enforcement-related items")
    return rows


# --------------------------- FSA Oman (former CMA Oman) ---------------------
def scrape_fsa_oman():
    """CMA Oman renamed to FSA Oman (fsa.gov.om); explore a couple of paths."""
    BASE = "https://fsa.gov.om"
    rows = []
    for path in ("/en/", "/Home/EnforcementAction", "/Home/LegalViolation", "/en/enforcement"):
        url = BASE + path
        try:
            r = requests.get(url, headers=HDRS, timeout=12, verify=False)
        except Exception as e:
            log("fsa_om", f"{url} {type(e).__name__}")
            continue
        if r.status_code != 200 or len(r.content) < 5000:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        # Look for any anchors that link to enforcement-action-like pages
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            href = a["href"]
            if not text or len(text) < 10:
                continue
            low = text.lower()
            if any(k in low for k in ("enforcement", "violation", "penalty", "fine", "sanction")):
                rows.append(base(
                    "Financial Services Authority Oman (FSA Oman)", "Enforcement Actions",
                    text[:200],
                    details=f"source_path: {path}",
                    detail_page_url=urljoin(BASE, href),
                ))
    # dedup by name
    seen = set(); uniq = []
    for r in rows:
        if r["name"] in seen: continue
        seen.add(r["name"]); uniq.append(r)
    log("fsa_om", f"{len(uniq)} entries")
    return uniq


def main():
    rows = scrape_cbuae();       p, n = write_csv(rows, "cbuae_enforcement.csv"); log("write", f"{n} -> {p}")
    rows = scrape_sca_uae();     p, n = write_csv(rows, "sca_uae_enforcement.csv"); log("write", f"{n} -> {p}")
    rows = scrape_cbb_bahrain(); p, n = write_csv(rows, "cbb_bahrain_enforcement.csv"); log("write", f"{n} -> {p}")
    rows = scrape_fsa_oman();    p, n = write_csv(rows, "cma_oman_enforcement.csv"); log("write", f"{n} -> {p}")


if __name__ == "__main__":
    main()
