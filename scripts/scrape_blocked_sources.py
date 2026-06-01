#!/usr/bin/env python3
"""Batch scrape sources currently registered with status=blocked + no scraper.

One-shot approach for the ~65 newly added blocked sources from the 944->1016
expansion. Each source gets at most 30s of wall-clock budget.

Strategy per source (tried in order, stop at first that yields rows):
  1. requests.get + BeautifulSoup → biggest <table> → rows
  2. If the page has a single PDF link → download → pdfplumber tables
  3. Playwright headless render → biggest <table> after networkidle

Output:
  - data/blocked_<source_id>.csv per source that yielded ≥1 row
  - scripts/scrape_blocked_sources.summary.json (success/fail breakdown)

Schema: the 17-column canonical schema. We pack everything beyond `name`
into `details` as "header: value | header: value" so no extracted column
is silently lost. detail_page_url is always the source's registered URL.
"""
from __future__ import annotations
import csv
import json
import os
import re
import signal
import sys
import time
import traceback
import urllib3
import warnings
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


def _fresh_get(url, **kw):
    """One-shot GET on a fresh Session that closes its connection on exit.
    urllib3's default connection pool grows globally across _fresh_get()
    calls; if any one source eats a long ConnectTimeout the pool can poison
    every subsequent call (instant 0.0s ConnectionError). A throw-away
    Session sidesteps that — each source pays its own connection cost but
    no cross-source corruption."""
    s = requests.Session()
    s.headers.update({"Connection": "close"})
    try:
        return s.get(url, **kw)
    finally:
        s.close()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SOURCES_JSON = os.path.join(PROJECT_ROOT, "sources.json")
SUMMARY_PATH = os.path.join(PROJECT_ROOT, "scripts",
                            "scrape_blocked_sources.summary.json")

os.makedirs(DATA_DIR, exist_ok=True)

CANONICAL_COLS = [
    "source_agency", "source_list", "case_unit", "name", "father_name",
    "date_of_birth", "gender", "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url", "interpol_notice_id",
    "link_kind", "scraped_at", "enrichment_status",
]

H_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Tokens we'll match in column headers to identify a name column.
NAME_HEADERS = ("name", "company", "entity", "individual", "firm", "person",
                "applicant", "respondent", "defendant", "borrower", "promoter",
                "officer", "director", "nominee", "issuer", "trustee",
                "intermediary", "registrant", "subject", "denomination",
                "razão", "nombre", "nome", "denominazione", "wanted", "fugitive")
# Tokens that disqualify a column from being treated as the entity name.
NEG_NAME_HEADERS = ("date", "year", "no.", "no ", "sl.", "sr.", "amount",
                    "section", "order", "case no", "fine", "penalty",
                    "status", "remarks", "notes", "comments", "regn",
                    "type", "category")

PER_SOURCE_TIMEOUT_S = 30
HTTP_TIMEOUT_S = 25
MIN_TABLE_ROWS = 2
MAX_ROWS_PER_SOURCE = 5000  # safety cap


# ────────────────────────────────────────────────────────────────────────────
# Time-budget helper

class Deadline(Exception):
    pass


def _alarm_handler(signum, frame):
    raise Deadline("source wall-clock budget exceeded")


def _with_deadline(seconds: int, fn, *args, **kw):
    """Run fn under a SIGALRM deadline. Raises Deadline if exceeded."""
    old = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(seconds)
    try:
        return fn(*args, **kw)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ────────────────────────────────────────────────────────────────────────────
# HTML table extraction

def _row(agency: str, list_name: str, name: str, *,
         details: str = "", detail_page_url: str = "",
         document_url: str = "", **kw) -> dict:
    base = {f: "" for f in CANONICAL_COLS}
    base.update(
        source_agency=agency,
        source_list=list_name,
        name=(name or "").strip()[:200],
        details=details[:1000],
        detail_page_url=detail_page_url,
        document_url=document_url,
        has_document="Yes" if document_url else ("Yes" if detail_page_url else "No"),
        link_kind="document" if document_url else "page",
        scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    for k, v in kw.items():
        if k in CANONICAL_COLS and v is not None:
            base[k] = str(v).strip()
    return base


def _table_score(t) -> int:
    """Heuristic: prefer tables with more <tr> rows and at least 2 columns."""
    rows = t.find_all("tr")
    if len(rows) < MIN_TABLE_ROWS + 1:  # need header + data
        return 0
    cols_first_row = len(rows[0].find_all(["th", "td"]))
    if cols_first_row < 2:
        return 0
    return len(rows) * cols_first_row


def _detect_name_col(headers: list[str]) -> int | None:
    """Find the most-likely name column by token match. Return index or None."""
    hh = [(i, (h or "").strip().lower()) for i, h in enumerate(headers)]
    # Positive matches that aren't disqualified.
    candidates = []
    for i, h in hh:
        if any(neg in h for neg in NEG_NAME_HEADERS):
            continue
        if any(tok in h for tok in NAME_HEADERS):
            candidates.append((i, h))
    if candidates:
        # Prefer the leftmost, but skip serial-number columns (sl., sr., no.)
        return candidates[0][0]
    return None


def _extract_rows_from_table(t, agency: str, list_name: str,
                              detail_url: str) -> list[dict]:
    """Pull rows from a single <table>. Header is first <tr>; data follows."""
    trs = t.find_all("tr")
    if len(trs) < MIN_TABLE_ROWS + 1:
        return []

    def cells(tr):
        return [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]

    headers = cells(trs[0])
    if not headers or len(headers) < 2:
        return []

    name_col = _detect_name_col(headers)
    rows = []
    for tr in trs[1:]:
        c = cells(tr)
        if not c or len(c) < 2:
            continue
        # Pad short rows so column alignment doesn't drop content.
        while len(c) < len(headers):
            c.append("")
        # Pick the name: detected col, or fall back to first non-numeric cell.
        if name_col is not None and name_col < len(c):
            name = c[name_col]
        else:
            name = next((cell for cell in c
                         if cell and not cell.replace(".", "").isdigit()),
                        c[0])
        if not name or len(name) < 2:
            continue
        # Pack remaining columns into details.
        bits = []
        for h, v in zip(headers, c):
            if not v:
                continue
            if h.lower() in {"name", ""}:
                continue
            bits.append(f"{h}: {v[:120]}")
        details = " | ".join(bits)[:1000]
        rows.append(_row(agency, list_name, name,
                         details=details, detail_page_url=detail_url))
        if len(rows) >= MAX_ROWS_PER_SOURCE:
            break
    return rows


def _try_html(source: dict) -> list[dict]:
    url = source["url"]
    r = _fresh_get(url, headers=H_BROWSER, timeout=HTTP_TIMEOUT_S,
                     verify=False, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return []
    # Sort tables by score desc, try each until one yields rows.
    ranked = sorted(((_table_score(t), t) for t in tables),
                    key=lambda x: -x[0])
    for score, t in ranked:
        if score == 0:
            break
        rows = _extract_rows_from_table(t, source["agency"],
                                         source["list_name"], url)
        if rows:
            return rows
    return []


# ────────────────────────────────────────────────────────────────────────────
# PDF fallback

def _try_pdf(source: dict) -> list[dict]:
    """If the page has exactly one (or a clearly-primary) PDF link, download
    it and extract tables via pdfplumber. Conservative: only attempts when
    one PDF is obvious to avoid downloading hundreds of PDFs per source."""
    url = source["url"]
    r = _fresh_get(url, headers=H_BROWSER, timeout=HTTP_TIMEOUT_S,
                     verify=False, allow_redirects=True)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    pdf_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") or ".pdf?" in href.lower():
            pdf_links.append(urljoin(url, href))
    pdf_links = list(dict.fromkeys(pdf_links))  # dedup, preserve order
    if not pdf_links or len(pdf_links) > 5:
        return []  # too many → ambiguous, skip
    try:
        import pdfplumber
    except ImportError:
        return []
    rows: list[dict] = []
    for pdf_url in pdf_links[:2]:  # at most 2 PDFs
        try:
            pr = _fresh_get(pdf_url, headers=H_BROWSER,
                              timeout=HTTP_TIMEOUT_S, verify=False)
            if pr.status_code != 200 or not pr.content.startswith(b"%PDF"):
                continue
            local = os.path.join(DATA_DIR,
                                 f"_tmp_blocked_{source['id']}.pdf")
            with open(local, "wb") as f:
                f.write(pr.content)
            with pdfplumber.open(local) as pdf:
                for page in pdf.pages[:20]:
                    for tbl in (page.extract_tables() or []):
                        if not tbl or len(tbl) < MIN_TABLE_ROWS + 1:
                            continue
                        headers = [str(x or "").strip() for x in tbl[0]]
                        name_col = _detect_name_col(headers)
                        for r2 in tbl[1:]:
                            r2 = [str(x or "").strip() for x in r2]
                            if not any(r2):
                                continue
                            while len(r2) < len(headers):
                                r2.append("")
                            if name_col is not None and name_col < len(r2):
                                name = r2[name_col]
                            else:
                                name = r2[0]
                            if not name or len(name) < 2:
                                continue
                            bits = [f"{h}: {v[:120]}"
                                    for h, v in zip(headers, r2)
                                    if v and h]
                            rows.append(_row(
                                source["agency"], source["list_name"], name,
                                details=" | ".join(bits)[:1000],
                                detail_page_url=url, document_url=pdf_url))
                            if len(rows) >= MAX_ROWS_PER_SOURCE:
                                break
                    page.flush_cache()
                    if len(rows) >= MAX_ROWS_PER_SOURCE:
                        break
            os.unlink(local)
            if rows:
                break
        except Exception:
            continue
    return rows


# ────────────────────────────────────────────────────────────────────────────
# Playwright fallback (off by default — Chromium is heavy)

def _try_playwright(source: dict) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []
    url = source["url"]
    rows: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=H_BROWSER["User-Agent"])
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=20_000)
            except Exception:
                page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            html = page.content()
            browser.close()
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return []
        ranked = sorted(((_table_score(t), t) for t in tables),
                        key=lambda x: -x[0])
        for score, t in ranked:
            if score == 0:
                break
            rows = _extract_rows_from_table(t, source["agency"],
                                             source["list_name"], url)
            if rows:
                return rows
    except Exception:
        return []
    return rows


# ────────────────────────────────────────────────────────────────────────────
# Per-source scrape

def _scrape_one(source: dict, allow_playwright: bool = False) -> tuple[list[dict], str]:
    """Try strategies. Returns (rows, strategy_used). Strategy is empty on fail."""
    try:
        rows = _try_html(source)
        if rows:
            return rows, "html_table"
    except Exception as e:
        # HTML attempt failed — fall through to PDF/playwright
        last_html_err = f"html {type(e).__name__}: {str(e)[:80]}"
    else:
        last_html_err = "html no table"
    try:
        rows = _try_pdf(source)
        if rows:
            return rows, "pdf"
    except Exception as e:
        last_pdf_err = f"pdf {type(e).__name__}: {str(e)[:80]}"
    else:
        last_pdf_err = "pdf no link"
    if allow_playwright:
        try:
            rows = _try_playwright(source)
            if rows:
                return rows, "playwright"
        except Exception as e:
            return [], f"playwright {type(e).__name__}: {str(e)[:80]}"
        else:
            return [], f"{last_html_err} ; {last_pdf_err} ; playwright no table"
    return [], f"{last_html_err} ; {last_pdf_err}"


# ────────────────────────────────────────────────────────────────────────────
# Main

def _write_csv(rows: list[dict], path: str) -> int:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CANONICAL_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CANONICAL_COLS})
    return len(rows)


def _pick_targets(only_new: bool = True) -> list[dict]:
    """Sources where status=blocked AND no scraper field set AND no data in DB."""
    import psycopg2
    with open(SOURCES_JSON) as f:
        data = json.load(f)
    conn = psycopg2.connect(host="localhost", user="aayush",
                             password="aayush123", dbname="risk_pipeline")
    cur = conn.cursor()
    cur.execute("SELECT source_id, COUNT(*) FROM watchlist_records "
                "GROUP BY source_id;")
    db_counts = dict(cur.fetchall())
    conn.close()
    targets = []
    for s in data["sources"]:
        if s.get("status") != "blocked":
            continue
        if not s.get("url"):
            continue
        if only_new and s.get("scraper"):
            continue
        if db_counts.get(s["id"], 0) > 0:
            continue
        targets.append(s)
    return targets


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--playwright", action="store_true",
                    help="enable Playwright fallback for JS-heavy pages")
    ap.add_argument("--max", type=int, default=0,
                    help="stop after N sources (0 = all)")
    ap.add_argument("--only", help="run only this source_id")
    args = ap.parse_args()

    targets = _pick_targets(only_new=True)
    if args.only:
        targets = [t for t in targets if t["id"] == args.only]
    if args.max > 0:
        targets = targets[:args.max]
    print(f"Targeting {len(targets)} blocked sources "
          f"(playwright={args.playwright})", flush=True)

    summary = {"started_at": datetime.now(timezone.utc).isoformat(),
               "playwright_enabled": args.playwright,
               "results": []}
    successes = []  # list of (source_id, csv_path, n_rows)

    for i, src in enumerate(targets, 1):
        sid = src["id"]
        url = src["url"]
        print(f"\n[{i}/{len(targets)}] {sid}", flush=True)
        print(f"    URL: {url[:100]}", flush=True)
        t0 = time.time()
        # Per-request HTTP_TIMEOUT_S is enough; SIGALRM-driven deadlines
        # leave urllib3's pool in a broken state if they fire mid-request
        # (every subsequent source then gets instant ConnectionError),
        # so no SIGALRM here.
        try:
            rows, strategy = _scrape_one(
                src, allow_playwright=args.playwright)
        except Exception as e:
            elapsed = time.time() - t0
            print(f"    ERR ({elapsed:.1f}s): {type(e).__name__}: {e}",
                  flush=True)
            summary["results"].append({
                "id": sid, "status": "error",
                "rows": 0, "strategy": "",
                "error": f"{type(e).__name__}: {str(e)[:160]}",
                "elapsed_s": round(elapsed, 1)})
            continue
        elapsed = time.time() - t0
        if not rows:
            print(f"    NO ROWS ({elapsed:.1f}s) — {strategy}", flush=True)
            summary["results"].append({
                "id": sid, "status": "empty",
                "rows": 0, "strategy": strategy,
                "elapsed_s": round(elapsed, 1)})
            continue
        out_path = os.path.join(DATA_DIR, f"blocked_{sid}.csv")
        n = _write_csv(rows, out_path)
        print(f"    OK ({elapsed:.1f}s) {strategy}: {n} rows -> {out_path}",
              flush=True)
        summary["results"].append({
            "id": sid, "status": "success",
            "rows": n, "strategy": strategy,
            "csv": out_path, "elapsed_s": round(elapsed, 1)})
        successes.append((sid, out_path, n))

    # Stats
    n_ok = sum(1 for r in summary["results"] if r["status"] == "success")
    n_empty = sum(1 for r in summary["results"] if r["status"] == "empty")
    n_err = sum(1 for r in summary["results"] if r["status"] == "error")
    n_dl = sum(1 for r in summary["results"] if r["status"] == "deadline")
    total_rows = sum(r["rows"] for r in summary["results"])
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["totals"] = {"success": n_ok, "empty": n_empty,
                          "error": n_err, "deadline": n_dl,
                          "total_rows": total_rows}
    os.makedirs(os.path.dirname(SUMMARY_PATH), exist_ok=True)
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n=== SUMMARY ===\n"
          f"  success     {n_ok}\n"
          f"  empty       {n_empty}\n"
          f"  error       {n_err}\n"
          f"  deadline    {n_dl}\n"
          f"  total rows  {total_rows}\n"
          f"  summary -> {SUMMARY_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
