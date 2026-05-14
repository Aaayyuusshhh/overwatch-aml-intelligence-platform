"""
BOI Wilful Defaulters scraper (#131).

Source: a scanned 24-page PDF from
  https://bankofindia.co.in/documents/20121/135570/List-of-WD-of-Bank-for-Website-Publication-31-12-2024.pdf

The PDF has no text layer (extract_text() returns 0 chars) so it must
be OCR'd. The generic engine's pdf_ocr strategy concatenated each
visual OCR line into a single record's name+details, producing rows
like "12 CHANDIGARH PANCHKULA 101293904 SANDEEP SHARMA SANDEEP
SHARMA 81.78 30/04/2008 30/06/2010" in the name field.

This scraper:
  1. Downloads (or reuses local) PDF.
  2. Rasterises each page at 250 dpi.
  3. Uses pytesseract.image_to_data to recover per-word bounding
     boxes so columns can be reconstructed by x-coordinate.
  4. Clusters words into visual lines (by y), then groups visual
     lines into table rows anchored on the leading SN number in the
     left-most column. Continuation lines (no SN) attach to the
     previous table row.
  5. Assigns each word to one of 9 columns by x-coordinate, scaled
     to page width:
       SN | Zone | Branch | Cust ID | Company | Partners | Outstanding | NPA Date | Declared Date
  6. Emits one watchlist record per defaulter row where
       name    = Company (or Partners, if Company empty)
       address = "Zone, Branch"
       details = pipe-separated metadata (Account / Amount Lakhs /
                 NPA Date / Declared Date / Partners)
"""

import csv
import os
import re
import time
from datetime import datetime

import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from pytesseract import Output
from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_URL = (
    "https://bankofindia.co.in/documents/20121/135570/"
    "List-of-WD-of-Bank-for-Website-Publication-31-12-2024.pdf"
)
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "raw", "boi_wilful_defaulters_131.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "boi_wilful_defaulters_131.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

OCR_DPI = 300
# Normalised column boundaries (fraction of page width). Derived from
# empirical bbox analysis of page 3 (image width 2066px).
#   col index -> (start, end) fraction
COL_BOUNDS = [
    (0.00, 0.097),   # 0 SN
    (0.097, 0.20),   # 1 Zone
    (0.20, 0.30),    # 2 Branch
    (0.30, 0.395),   # 3 Cust ID
    (0.395, 0.552),  # 4 Company name
    (0.552, 0.726),  # 5 Partners / proprietors
    (0.726, 0.803),  # 6 Outstanding (Lakhs)
    (0.803, 0.877),  # 7 NPA Date
    (0.877, 1.001),  # 8 Declared Date
]
COL_NAMES = ["sn", "zone", "branch", "cust", "company",
             "partners", "amount", "npa_date", "decl_date"]

# Y-tolerance (px) for clustering words into the same visual line.
LINE_Y_TOL = 14
# Min OCR confidence to keep a word (0-100). Tesseract gives -1 for noise.
MIN_CONF = 20
# Words that show up as page chrome to filter out
NOISE_TEXTS = {
    "|", "_", "-", ".", "/", ":", ";", "—", "_|", "|_", "[", "]",
    "{", "}", "=", "—_", "~", "*", "—",
}
DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,3})?$")
SN_RE = re.compile(r"^\d{1,4}$")
ACCT_RE = re.compile(r"^\d{9,12}$")
HEADER_KEYWORDS = (
    "bank of india", "head office", "recovery department",
    "names of borrowers", "names of directors", "outstanding",
    "wilful defaulter", "partners /", "in lakhs", "sn zone",
    "date of", "declaring as", "as on npa", "31.12.2024",
)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _download_pdf():
    """Fetch the PDF if not already on disk. Reuses cached copy when
    possible to keep iterations fast."""
    if os.path.exists(PDF_PATH) and os.path.getsize(PDF_PATH) > 100_000:
        return PDF_PATH
    os.makedirs(os.path.dirname(PDF_PATH), exist_ok=True)
    print(f"  downloading {SOURCE_URL}")
    r = Fetcher.get(SOURCE_URL, timeout=120, retries=2,
                    retry_delay=2, verify=False)
    body = getattr(r, "body", None) or getattr(r, "content", None)
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    if not body or not body[:8].lstrip().startswith(b"%PDF"):
        raise RuntimeError("BOI: downloaded payload is not a PDF")
    with open(PDF_PATH, "wb") as f:
        f.write(body)
    return PDF_PATH


def _strip_punct(word):
    """Remove leading/trailing OCR punctuation noise so SN/account
    detection works even when tesseract glues a '|' or ']' to the
    digit."""
    return re.sub(r"^[^\w./]+|[^\w./]+$", "", word)


def _word_column(x_centre, page_width):
    frac = x_centre / max(1, page_width)
    for idx, (a, b) in enumerate(COL_BOUNDS):
        if a <= frac < b:
            return idx
    return len(COL_BOUNDS) - 1


def _extract_page_rows(img):
    """Return list of dicts, one per table row on this page. Each
    dict maps column-name -> joined word text. Multi-OCR-line cells
    are concatenated with spaces.

    Row boundaries are detected by the leading SN word (a 1-4 digit
    integer in the SN column). Continuation OCR lines (no SN) merge
    into the previous row."""
    data = pytesseract.image_to_data(
        img, output_type=Output.DICT, config="--psm 6"
    )
    page_w = img.size[0]
    n = len(data["text"])

    # Collect words with usable text + position.
    words = []
    for i in range(n):
        txt = data["text"][i]
        if not txt:
            continue
        txt = txt.strip()
        if not txt or txt in NOISE_TEXTS:
            continue
        try:
            conf = int(float(data["conf"][i]))
        except (TypeError, ValueError):
            conf = -1
        if conf >= 0 and conf < MIN_CONF:
            continue
        x = data["left"][i]
        y = data["top"][i]
        h = data["height"][i]
        w = data["width"][i]
        words.append({
            "txt": txt,
            "clean": _strip_punct(txt),
            "x": x,
            "y": y,
            "x_c": x + w / 2,
            "y_c": y + h / 2,
            "h": h,
        })
    if not words:
        return []

    # Cluster words into visual lines by y-centre.
    words.sort(key=lambda w: (w["y_c"], w["x"]))
    lines = []
    current = []
    line_y = None
    for w in words:
        if not current:
            current = [w]
            line_y = w["y_c"]
            continue
        if abs(w["y_c"] - line_y) <= LINE_Y_TOL:
            current.append(w)
            # Use running mean so the line drifts with content
            line_y = (line_y * (len(current) - 1) + w["y_c"]) / len(current)
        else:
            current.sort(key=lambda x: x["x"])
            lines.append(current)
            current = [w]
            line_y = w["y_c"]
    if current:
        current.sort(key=lambda x: x["x"])
        lines.append(current)

    # Walk lines top-to-bottom. A line starts a new table row when EITHER
    #   (a) its left-most word is a SN-shaped integer (1-4 digits) in the
    #       SN column, OR
    #   (b) it contains an account-number-shaped word in the cust column
    #       and the current row already has its account filled. This
    #       recovers from cases where tesseract failed to recognise the
    #       leading SN digit, which would otherwise merge two table rows.
    rows = []
    current_row = None
    for ln in lines:
        # Detect any header-noise line
        joined = " ".join(w["clean"] for w in ln).lower()
        if any(k in joined for k in HEADER_KEYWORDS):
            continue
        first = ln[0]
        sn_col = _word_column(first["x_c"], page_w) == 0
        sn_like = SN_RE.match(first["clean"] or "") is not None

        # Detect a fresh account number anywhere on this line in cust col.
        new_acct = None
        for w in ln:
            if (ACCT_RE.match(w["clean"] or "")
                    and _word_column(w["x_c"], page_w) == 3):
                new_acct = w["clean"]
                break

        starts_new = False
        if sn_col and sn_like:
            starts_new = True
        elif (current_row is not None and new_acct
              and current_row.get("cust")):
            starts_new = True
        elif current_row is None and new_acct:
            # First data row on the page where the SN was OCR'd-away.
            starts_new = True

        if starts_new:
            if current_row:
                rows.append(current_row)
            current_row = {c: [] for c in COL_NAMES}
            for w in ln:
                # Skip the SN word itself when populating (it goes in sn).
                if w is first and sn_col and sn_like:
                    current_row["sn"].append(w["clean"])
                    continue
                _assign_word(current_row, w, page_w)
        else:
            if current_row is None:
                continue
            for w in ln:
                _assign_word(current_row, w, page_w)
    if current_row:
        rows.append(current_row)

    # Stringify
    out = []
    for r in rows:
        out.append({c: _clean(" ".join(r[c])) for c in COL_NAMES})
    return out


def _assign_word(row, w, page_w):
    """Place a word into the appropriate column bucket. Date- and
    amount-shaped tokens are pulled into their canonical columns even
    if OCR x is slightly off."""
    txt = w["clean"]
    if not txt:
        return
    col_idx = _word_column(w["x_c"], page_w)
    # Sanity overrides by token shape
    if DATE_RE.fullmatch(txt):
        # First date -> NPA, second -> Declared. Decide by which slot empty.
        if not row["npa_date"]:
            row["npa_date"].append(txt)
        else:
            row["decl_date"].append(txt)
        return
    if AMOUNT_RE.match(txt):
        # Treat as amount only if no dates yet seen (amount comes before
        # the two trailing date columns) and the column index suggests it.
        if col_idx in (6, 7) and not row["amount"]:
            row["amount"].append(txt)
            return
    if ACCT_RE.match(txt) and not row["cust"]:
        row["cust"].append(txt)
        return
    row[COL_NAMES[col_idx]].append(txt)


def _normalise_amount(raw):
    s = (raw or "").replace(",", "").replace("|", "").strip()
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return ""
    # Some OCR'd amounts split the decimal: '402 85' style. We don't
    # try to repair those — keep as-is.
    return s


def _normalise_date(raw):
    s = (raw or "").strip()
    m = DATE_RE.search(s)
    if not m:
        return ""
    return m.group(0).replace("|", "")


def _strip_artifacts(s):
    """Strip leading/trailing OCR pipe/underscore/bracket clutter."""
    if not s:
        return s
    s = re.sub(r"[\|_\[\]{}~=]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .,:;-")
    return s


def _build_record(row, scraped_at):
    company = _strip_artifacts(_clean(row.get("company")))
    partners = _strip_artifacts(_clean(row.get("partners")))
    zone = _strip_artifacts(_clean(row.get("zone")))
    branch = _strip_artifacts(_clean(row.get("branch")))
    cust = _clean(row.get("cust"))
    cust = re.sub(r"[^\d]", "", cust) if cust else ""
    amount = _normalise_amount(row.get("amount"))
    npa = _normalise_date(row.get("npa_date"))
    decl = _normalise_date(row.get("decl_date"))

    # Name: company if present, else first partner. Strip stray punctuation.
    if company:
        name = company
    elif partners:
        name = partners.split(",")[0].strip()
    else:
        name = ""
    name = re.sub(r"\s+", " ", name).strip(" |:.,/-")
    # Drop rows that are obvious OCR garbage: too short, no letters, or
    # no useful fields at all.
    if not name or len(name) < 4 or not re.search(r"[A-Za-z]{2,}", name):
        return None
    if not (cust or amount or company or partners or npa or decl):
        return None

    address = ", ".join(p for p in (zone, branch) if p)

    detail_parts = []
    if cust:
        detail_parts.append(f"Account: {cust}")
    if amount:
        detail_parts.append(f"Amount: {amount} Lakhs")
    if partners and partners.lower() != name.lower():
        detail_parts.append(f"Partners/Directors: {partners}")
    if npa or decl:
        detail_parts.append(f"Date: {npa or '?'} - {decl or '?'}")

    return {
        "source_agency": "Bank of India (BOI)",
        "source_list": "Wilful Defaulters",
        "case_unit": cust,
        "name": name,
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": address,
        "reward_amount": amount,
        "details": " | ".join(detail_parts),
        "has_document": "Yes",
        "document_url": SOURCE_URL,
        "detail_page_url": SOURCE_URL,
        "interpol_notice_id": "",
        "link_kind": "pdf_ocr_columnar",
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


def scrape():
    pdf_path = _download_pdf()
    print(f"  using PDF: {pdf_path}")
    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
    print(f"  pages: {n_pages}")

    scraped_at = _now()
    all_rows = []
    t_start = time.time()
    # Rasterise + OCR page-by-page to keep memory bounded.
    for pno in range(1, n_pages + 1):
        t0 = time.time()
        imgs = convert_from_path(pdf_path, dpi=OCR_DPI,
                                 first_page=pno, last_page=pno)
        if not imgs:
            continue
        page_rows = _extract_page_rows(imgs[0])
        kept = 0
        for r in page_rows:
            rec = _build_record(r, scraped_at)
            if rec:
                all_rows.append(rec)
                kept += 1
        print(f"  page {pno:2d}: ocr_rows={len(page_rows)} kept={kept} "
              f"({time.time()-t0:.1f}s)")
    print(f"  total runtime: {time.time()-t_start:.1f}s")
    return all_rows


def save_to_csv(rows, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {out_path}")


def run():
    print("=" * 60)
    print("BOI Wilful Defaulters scraper (#131)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("BOI: zero rows extracted")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
