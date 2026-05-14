"""
engines/html_scraper.py - Generic HTML scraper for sources without a
custom scraper.

Per ARCHITECTURE.md S4.3 / PRD S6.2. Cascading extraction:

  Strategy 1 - tables  : pick the largest <table>, parse rows
  Strategy 2 - blocks  : extract repeating divs/articles/li/dl groups
  Strategy 3 - raw text: dump first 5000 chars as a single row

All output writes the 17-column shared schema. New link_kind values:
  - html_generic   (table extraction)
  - html_block     (block extraction)
  - unstructured   (raw text fallback)

Public entry point:
    run(source_config) -> result_dict
"""

import csv
import os
import re
import time
from datetime import datetime

from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

FETCH_TIMEOUT = 30          # seconds, per single fetch
RAW_TEXT_LIMIT = 5000       # chars saved by the unstructured fallback
MIN_TABLE_ROWS = 4          # >= header + 3 data rows; demotes search forms / nav tables
POLITENESS_SECONDS = 2.0    # self-paced delay between consecutive engine runs

# Phrases that signal the page is bot-blocked / login-walled / JS-required.
# Match against lower-cased visible text. If any are present, the source
# is short-circuited to status=skipped with reason in the error field.
BOT_BLOCK_PHRASES = (
    "request rejected",
    "access denied",
    "please enable javascript",
    "javascript is required",
    "captcha required",
    "verify you are human",
    "are you a robot",
    "checking your browser",
    "human verification",
    "the requested url was rejected",
)

# Header-text -> schema-column matchers. Lower-case substring matches.
NAME_HEADERS = ("name", "person", "entity", "company", "firm",
                "individual", "accused", "defaulter", "borrower",
                "consultant", "contractor", "vendor", "agency",
                "organisation", "organization")
FATHER_HEADERS = ("father", "parent", "guardian", "s/o", "d/o", "w/o")
DOB_HEADERS = ("birth", "dob", "date of birth")
GENDER_HEADERS = ("gender", "sex")
ADDRESS_HEADERS = ("address", "residence", "location", "city", "state",
                   "district", "country")
REWARD_HEADERS = ("reward", "amount", "penalty", "fine")
CASE_HEADERS = ("case", "fir", "rc no", "ref", "reference", "order no",
                "case no")
DOCUMENT_HEADERS = ("document", "pdf", "attachment", "order", "judgement",
                    "judgment", "notice", "circular")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(s):
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _output_path(source_id):
    return os.path.join(DATA_DIR, f"{source_id}.csv")


def _empty_row(agency, source_list, scraped_at):
    return {
        "source_agency": agency,
        "source_list": source_list,
        "case_unit": "",
        "name": "",
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": "",
        "has_document": "No",
        "document_url": "",
        "detail_page_url": "",
        "interpol_notice_id": "",
        "link_kind": "",
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


def _save_csv(rows, out_path):
    if not rows:
        return 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    return len(rows)


def _match_header(header_lc, candidates):
    return any(c in header_lc for c in candidates)


def _classify_headers(headers):
    """Map each table-header index to a 17-column-schema field name (or
    None for 'leave for details')."""
    mapping = {}
    name_idx = None
    for i, h in enumerate(headers):
        h_lc = (h or "").lower()
        if not h_lc:
            continue
        if name_idx is None and _match_header(h_lc, NAME_HEADERS):
            mapping[i] = "name"
            name_idx = i
        elif _match_header(h_lc, FATHER_HEADERS):
            mapping[i] = "father_name"
        elif _match_header(h_lc, DOB_HEADERS):
            mapping[i] = "date_of_birth"
        elif _match_header(h_lc, GENDER_HEADERS):
            mapping[i] = "gender"
        elif _match_header(h_lc, ADDRESS_HEADERS):
            mapping[i] = "address"
        elif _match_header(h_lc, REWARD_HEADERS):
            mapping[i] = "reward_amount"
        elif _match_header(h_lc, CASE_HEADERS):
            mapping[i] = "case_unit"
        elif _match_header(h_lc, DOCUMENT_HEADERS):
            mapping[i] = "document_url"
    return mapping, name_idx


# ---------------------------------------------------------------------------
# Strategy 1: TABLE EXTRACTION
# ---------------------------------------------------------------------------
def _extract_tables(page):
    tables = page.find_all("table") or []
    if not tables:
        return None
    # Pick the table with the most <tr> children.
    best = None
    best_count = 0
    for t in tables:
        try:
            n = len(t.find_all("tr") or [])
        except Exception:
            n = 0
        if n > best_count:
            best = t
            best_count = n
    # Require >= header + 3 data rows. Smaller tables are usually
    # search forms or 1-2 row navigation widgets, not data.
    if best is None or best_count < MIN_TABLE_ROWS:
        return None
    return best


def _row_text_cells(row):
    cells = row.find_all("td") or row.find_all("th") or []
    return [_clean_text(c.get_all_text() if hasattr(c, "get_all_text")
                        else c.text) for c in cells]


def _extract_links_from_row(row, page_url):
    links = []
    for a in (row.find_all("a") or []):
        href = a.attrib.get("href", "") if hasattr(a, "attrib") else ""
        if not href or href.startswith("javascript:"):
            continue
        if href.startswith("/"):
            # Resolve against the original page URL's host.
            from urllib.parse import urljoin
            href = urljoin(page_url, href)
        links.append(href)
    return links


def strategy_table(page, source, scraped_at, page_url):
    table = _extract_tables(page)
    if table is None:
        return None

    rows = table.find_all("tr") or []
    if len(rows) < 2:
        return None

    # First row: headers (text) - if first row has any <th> use it as headers
    # otherwise fall back to "col_N".
    headers = _row_text_cells(rows[0])
    if not any(headers):
        headers = [f"col_{i}" for i in range(len(rows[0].find_all("td") or []))]

    header_map, name_idx = _classify_headers(headers)

    out = []
    for tr in rows[1:]:
        cells = _row_text_cells(tr)
        if not any(cells):
            continue
        if name_idx is None:
            # No matched 'name' header - use first non-empty cell.
            first_idx = next((i for i, c in enumerate(cells) if c), None)
            if first_idx is None:
                continue
            local_name_idx = first_idx
        else:
            local_name_idx = name_idx

        rec = _empty_row(source["agency"], source["list_name"], scraped_at)
        rec["link_kind"] = "html_generic"
        rec["name"] = cells[local_name_idx] if local_name_idx < len(cells) else ""

        details_pairs = []
        for i, val in enumerate(cells):
            if i == local_name_idx:
                continue
            field = header_map.get(i)
            header = headers[i] if i < len(headers) else f"col_{i}"
            if field and field != "name":
                if rec.get(field):
                    rec[field] = rec[field] + " | " + val
                else:
                    rec[field] = val
            else:
                if val:
                    details_pairs.append(f"{header}: {val}")

        # Capture row-level links: first as detail_page_url, document URLs
        # (PDFs) become document_url + has_document=Yes.
        links = _extract_links_from_row(tr, page_url)
        for link in links:
            if link.lower().endswith(".pdf") and not rec["document_url"]:
                rec["document_url"] = link
                rec["has_document"] = "Yes"
            elif not rec["detail_page_url"]:
                rec["detail_page_url"] = link

        if details_pairs:
            rec["details"] = " | ".join(details_pairs)

        if rec["name"]:
            out.append(rec)
    # Defense-in-depth: require >= 3 extracted data rows for table to count.
    if len(out) < 3:
        return None
    return out


# ---------------------------------------------------------------------------
# Strategy 2: BLOCK EXTRACTION
# ---------------------------------------------------------------------------
BLOCK_SELECTORS = [
    "article",
    "div.card", "div.list-item", "div.item", "div.entry",
    "li.card", "li.list-item", "li.item",
    "ul.list li", "ol.list li",
    "dl",
]


def _largest_repeating_group(page):
    """Pick whichever selector yields the most matches (>=3)."""
    best = None
    best_n = 0
    for sel in BLOCK_SELECTORS:
        try:
            elems = page.find_all(sel) or []
        except Exception:
            continue
        if len(elems) > best_n:
            best = elems
            best_n = len(elems)
    if best_n >= 3:
        return best
    return None


def strategy_blocks(page, source, scraped_at, page_url):
    blocks = _largest_repeating_group(page)
    if not blocks:
        return None
    out = []
    for block in blocks:
        try:
            text = _clean_text(block.get_all_text() if hasattr(block, "get_all_text")
                               else block.text)
        except Exception:
            continue
        if not text or len(text) < 20:
            continue
        rec = _empty_row(source["agency"], source["list_name"], scraped_at)
        rec["link_kind"] = "html_block"
        # First "line" or first 80 chars as name; remainder as details.
        first_line = text.split(" - ")[0].split("\n")[0].strip()[:200]
        rec["name"] = first_line
        rec["details"] = text[:2000]
        # First link in the block as detail_page_url.
        try:
            anchors = block.find_all("a") or []
            for a in anchors:
                href = a.attrib.get("href", "") if hasattr(a, "attrib") else ""
                if not href or href.startswith("javascript:"):
                    continue
                from urllib.parse import urljoin
                href_abs = urljoin(page_url, href)
                if href.lower().endswith(".pdf"):
                    rec["document_url"] = href_abs
                    rec["has_document"] = "Yes"
                elif not rec["detail_page_url"]:
                    rec["detail_page_url"] = href_abs
        except Exception:
            pass
        out.append(rec)
    return out if out else None


# ---------------------------------------------------------------------------
# Strategy 3: RAW TEXT FALLBACK
# ---------------------------------------------------------------------------
def strategy_raw_text(page, source, scraped_at, page_url):
    try:
        text = _clean_text(page.get_all_text() if hasattr(page, "get_all_text")
                           else (page.body if isinstance(page.body, str)
                                 else page.body.decode("utf-8", "replace")))
    except Exception:
        text = ""
    if not text:
        return None
    rec = _empty_row(source["agency"], source["list_name"], scraped_at)
    rec["link_kind"] = "unstructured"
    rec["name"] = ""
    rec["details"] = text[:RAW_TEXT_LIMIT]
    rec["detail_page_url"] = page_url
    return [rec]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
_last_run_at = 0.0


def _detect_bot_block(page):
    """Scan the page text for known bot-block / login-wall / JS-required
    phrases. Returns the matched phrase or None."""
    try:
        text = page.get_all_text() if hasattr(page, "get_all_text") else ""
    except Exception:
        text = ""
    if not text:
        # fall back to body html
        body = getattr(page, "body", None) or getattr(page, "html_content", "")
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        text = body or ""
    text_lc = text.lower()[:8000]
    for phrase in BOT_BLOCK_PHRASES:
        if phrase in text_lc:
            return phrase
    return None


def extract_from_page(page, source, page_url):
    """Run the cascade (table -> blocks -> raw_text) on a pre-fetched
    Scrapling page. Returns (rows_or_None, strategy_used_or_None).

    Used by run() (Tier 1, static fetch) and run_browser() (Tier 2,
    StealthyFetcher) - same extraction logic, different fetch."""
    scraped_at = _now()
    try:
        rows = strategy_table(page, source, scraped_at, page_url)
        if rows:
            return rows, "table"
    except Exception as e:
        print(f"  strategy_table raised {type(e).__name__}: {e}")
    try:
        rows = strategy_blocks(page, source, scraped_at, page_url)
        if rows:
            return rows, "blocks"
    except Exception as e:
        print(f"  strategy_blocks raised {type(e).__name__}: {e}")
    try:
        rows = strategy_raw_text(page, source, scraped_at, page_url)
        if rows:
            return rows, "raw_text"
    except Exception as e:
        print(f"  strategy_raw_text raised {type(e).__name__}: {e}")
    return None, None


def _make_failure(start, error, strategy=None, fetch_tier="static"):
    return {
        "status": "failure", "record_count": 0,
        "runtime_seconds": round(time.time() - start, 2),
        "error": error,
        "csv_path": None, "extraction_strategy": strategy,
        "fetch_tier": fetch_tier,
    }


def _make_skipped(start, error, strategy="bot_blocked", fetch_tier="static"):
    return {
        "status": "skipped", "record_count": 0,
        "runtime_seconds": round(time.time() - start, 2),
        "error": error,
        "csv_path": None, "extraction_strategy": strategy,
        "fetch_tier": fetch_tier,
    }


def run(source):
    """Tier-1 (static fetch) extraction path. Returns the framework
    result dict augmented with 'fetch_tier' = 'static'."""
    global _last_run_at

    # Self-paced politeness: ensure >= POLITENESS_SECONDS between
    # consecutive engine invocations across sources (per PRD S6.2).
    elapsed_since_last = time.time() - _last_run_at
    if elapsed_since_last < POLITENESS_SECONDS:
        time.sleep(POLITENESS_SECONDS - elapsed_since_last)

    start = time.time()
    sid = source["id"]
    url = source.get("url")
    out_path = _output_path(sid)

    if not url:
        _last_run_at = time.time()
        return _make_failure(start, "no url in source config")

    try:
        page = Fetcher.get(url, timeout=FETCH_TIMEOUT, retries=1,
                           retry_delay=0, verify=False)
    except Exception as e:
        _last_run_at = time.time()
        return _make_failure(start,
            f"fetch_error {type(e).__name__}: {str(e)[:120]}")

    status = getattr(page, "status", None) or getattr(page, "status_code", None)
    if status is None or status >= 400:
        _last_run_at = time.time()
        return _make_failure(start, f"http_status={status}")

    # Bot/block detection: short-circuit before extraction.
    blocked_phrase = _detect_bot_block(page)
    if blocked_phrase:
        _last_run_at = time.time()
        return _make_skipped(start,
            f"restricted: bot_block_detected ({blocked_phrase!r})")

    rows, strategy_used = extract_from_page(page, source, url)
    if not rows:
        _last_run_at = time.time()
        return _make_failure(start, "all strategies returned no rows")

    n = _save_csv(rows, out_path)
    _last_run_at = time.time()
    return {
        "status": "success",
        "record_count": n,
        "runtime_seconds": round(time.time() - start, 2),
        "error": None,
        "csv_path": out_path,
        "extraction_strategy": strategy_used,
        "fetch_tier": "static",
    }


def run_browser(source):
    """Tier-2 (browser-rendered fetch) extraction path. Uses
    StealthyFetcher; same extraction cascade as run(). Result dict
    has 'fetch_tier' = 'browser'."""
    from engines import browser_fetcher  # local import - guarded inside

    global _last_run_at
    start = time.time()
    sid = source["id"]
    url = source.get("url")
    out_path = _output_path(sid)

    if not url:
        return _make_failure(start, "no url in source config",
                              fetch_tier="browser")

    page, status, fetch_t = browser_fetcher.fetch_rendered(url)
    if page is None:
        # browser unavailable or hard error
        return _make_failure(start,
            f"browser_render_failed (fetch_t={fetch_t:.1f}s)",
            fetch_tier="browser")
    if status is None or (isinstance(status, int) and status >= 400):
        return _make_failure(start, f"browser http_status={status}",
                              fetch_tier="browser")

    blocked = _detect_bot_block(page)
    if blocked:
        return _make_skipped(start,
            f"restricted: bot_block_detected ({blocked!r})",
            fetch_tier="browser")

    rows, strategy_used = extract_from_page(page, source, url)
    if not rows:
        return _make_failure(start, "browser_render_insufficient",
                              fetch_tier="browser")

    n = _save_csv(rows, out_path)
    return {
        "status": "success",
        "record_count": n,
        "runtime_seconds": round(time.time() - start, 2),
        "error": None,
        "csv_path": out_path,
        "extraction_strategy": strategy_used,
        "fetch_tier": "browser",
    }
