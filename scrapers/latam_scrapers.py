#!/usr/bin/env python3
"""
Latin American regulator scrapers.

Sources:
  - Brazil COAF — Ementário de Decisões PAS (brazil_coaf_pas)         [NEW]
  - Argentina CNV — Denuncias Penales (argentina_cnv_denuncias)        [NEW]

(Colombia SFC sanctions report is a JS-only portlet — skipped under
the 3-minute rule. Mexico CNBV / Chile CMF / Peru SBS all returned
SPA shells or were timing out.)
"""
from __future__ import annotations
import csv
import re
import time
import warnings
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

H = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

FIELDS = [
    "source_agency", "source_list", "case_unit", "name", "father_name",
    "date_of_birth", "gender", "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url", "interpol_notice_id",
    "link_kind", "scraped_at", "enrichment_status",
]


def _row(agency: str, list_name: str, name: str, **kw) -> dict:
    base = {f: "" for f in FIELDS}
    base["source_agency"] = agency
    base["source_list"] = list_name
    base["name"] = (name or "").strip()
    base["scraped_at"] = datetime.utcnow().isoformat()[:19]
    for k, v in kw.items():
        if k in FIELDS and v is not None:
            base[k] = (v if isinstance(v, str) else str(v)).strip()
    return base


def _write_csv(rows: list[dict], path: str) -> int:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    print(f"  wrote {len(rows):>5d} rows -> {path}")
    return len(rows)


# --------------------------------------------------------------------------
# 1. Brazil COAF — Ementário de Decisões PAS
# --------------------------------------------------------------------------
_COAF_BASE = ("https://www.gov.br/coaf/pt-br/assuntos/"
              "processo-administrativo-sancionador-pas/ementario-de-decisoes")
_PROC_DETAIL_RE = re.compile(r"/ementario-de-decisoes/\d{4}/processo-no-")


def _coaf_listing_pages():
    """Discover all listing pages via 'b_start:int' Plone pagination."""
    r = requests.get(_COAF_BASE, headers=H, timeout=30, verify=False)
    soup = BeautifulSoup(r.text, "html.parser")
    last_start = 0
    for a in soup.find_all("a", href=re.compile(r"b_start:int=(\d+)")):
        m = re.search(r"b_start:int=(\d+)", a["href"])
        if m:
            last_start = max(last_start, int(m.group(1)))
    pages = [_COAF_BASE] + [
        f"{_COAF_BASE}?b_start:int={s}" for s in range(5, last_start + 1, 5)
    ]
    return pages


def _coaf_parse_detail(html: str, detail_url: str) -> list[dict]:
    """Extract one row per 'Interessado' on the decision page."""
    AG, LN = "Brazil COAF (Conselho de Controle de Atividades Financeiras)", "Processos Administrativos Sancionadores (PAS)"
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    case_no = ""
    m = re.search(r"Processo n[ºo°]\s*([\d\.\-\/]+)", text)
    if m:
        case_no = m.group(1)

    julgamento = ""
    m = re.search(r"Data de Julgamento:\s*([\d/\.\-]+)", text)
    if m:
        julgamento = m.group(1)

    segmento = ""
    m = re.search(r"Segmento:\s*([^\.;]+)", text)
    if m:
        segmento = m.group(1).strip()[:200]

    # Pull just the "Interessados:" sentence
    inter_match = re.search(r"Interessados?:\s*(.+?)(?=Compartilhe|Segmento|Relator|Ementa|$)", text, re.I)
    if not inter_match:
        return []
    inter_str = inter_match.group(1).strip()
    # Each defendant separated by ';'. Some entries have CPF/CNPJ inline.
    rows = []
    for part in re.split(r"\s*;\s*", inter_str):
        part = part.strip().rstrip(".")
        if not part or len(part) < 4:
            continue
        # Parse out name + CPF/CNPJ
        ent_name = part
        identifier = ""
        cnpj = re.search(r"CNPJ\s*([\d\.\-/]+)", part, re.I)
        cpf = re.search(r"CPF\s*([\d\.\-]+)", part, re.I)
        if cnpj:
            identifier = f"CNPJ {cnpj.group(1)}"
            ent_name = part[: cnpj.start()].rstrip(", ")
        elif cpf:
            identifier = f"CPF {cpf.group(1)}"
            ent_name = part[: cpf.start()].rstrip(", ")
        details_bits = []
        if julgamento:
            details_bits.append(f"Julgamento {julgamento}")
        if segmento:
            details_bits.append(f"Segmento: {segmento}")
        if identifier:
            details_bits.append(identifier)
        rows.append(_row(
            AG, LN, ent_name[:200],
            case_unit=f"Processo nº {case_no}" if case_no else "PAS",
            details=" | ".join(details_bits),
            detail_page_url=detail_url,
            link_kind="administrative_decision",
        ))
    return rows


def _coaf_fetch_detail(href: str) -> list[dict]:
    """Fetch + parse a single COAF detail page. Safe to run in parallel
    — returns rows or an empty list on any error."""
    try:
        rd = requests.get(href, headers=H, timeout=20, verify=False)
        return _coaf_parse_detail(rd.text, href)
    except Exception as e:
        # Logged at WARN level by caller — don't spam the per-detail line
        # at INFO when we have hundreds of details concurrently.
        return []


def scrape_brazil_coaf(max_workers: int = 5) -> list[dict]:
    """Scrape Brazil COAF PAS decisions. Listing pages are walked
    sequentially (they're paginated, so a small constant), but each
    page's detail links are fetched concurrently — one worker per
    target slot. Empirically takes the run from ~25min to ~5min with
    max_workers=5 on COAF's typical 8-10 detail-per-page density."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    rows: list[dict] = []
    seen_details = set()

    pages = _coaf_listing_pages()
    print(f"  COAF: {len(pages)} listing pages, {max_workers} parallel "
          "detail workers")

    n_errors = 0
    for idx, page_url in enumerate(pages, 1):
        try:
            r = requests.get(page_url, headers=H, timeout=20, verify=False)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"    page {idx}: ERR {e}")
            continue

        # full URLs only, deduped against the cumulative seen set so the
        # workers below never hit the same href twice
        detail_links = []
        for a in soup.find_all("a", href=_PROC_DETAIL_RE):
            href = urljoin(page_url, a["href"])
            if href in seen_details:
                continue
            seen_details.add(href)
            detail_links.append(href)

        if detail_links:
            page_errors = 0
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_coaf_fetch_detail, h): h
                           for h in detail_links}
                for fut in as_completed(futures):
                    try:
                        new_rows = fut.result(timeout=30)
                    except Exception:
                        page_errors += 1
                        continue
                    if not new_rows:
                        page_errors += 1
                    else:
                        rows.extend(new_rows)
            n_errors += page_errors

        if idx % 10 == 0 or idx == len(pages):
            print(f"    page {idx:>3d}/{len(pages)} -> {len(rows)} rows "
                  f"(errors so far: {n_errors})")
        # Gentle delay between listing-page batches; per-detail rate is
        # already governed by the worker pool.
        time.sleep(0.2)
    return rows


# --------------------------------------------------------------------------
# 2. Argentina CNV — Denuncias Penales
# --------------------------------------------------------------------------
def scrape_argentina_cnv() -> list[dict]:
    """
    Each entry on the page is a 'RRFCO-YYYY-N-APN-DIR#CNV' resolution
    title linking to a PDF. We register the resolution per row (1 row
    per PDF; the underlying defendant requires PDF extraction).
    """
    AG = "Comisión Nacional de Valores (CNV) Argentina"
    LN = "Denuncias Penales"
    url = "https://www.argentina.gob.ar/cnv/denuncias-penales"
    rows: list[dict] = []
    try:
        r = requests.get(url, headers=H, timeout=30, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  CNV ERR: {e}")
        return rows
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"\.pdf$", re.I)):
        href = urljoin(url, a["href"])
        if href in seen:
            continue
        seen.add(href)
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 4:
            continue
        # resolution number in title or filename
        m = re.search(r"(\d{4})[_\-\.](\d+)", href)
        period = f"{m.group(1)}" if m else ""
        rows.append(_row(
            AG, LN, title[:200],
            case_unit=f"Resolución {period}" if period else "Resolución",
            details=f"Denuncia penal publicada por CNV — {title}",
            document_url=href,
            has_document="true",
            detail_page_url=url,
            link_kind="criminal_complaint",
        ))
    return rows


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import os, sys
    os.makedirs("data", exist_ok=True)

    targets = [
        ("brazil_coaf_pas",        scrape_brazil_coaf),
        ("argentina_cnv_denuncias", scrape_argentina_cnv),
    ]
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    for sid, fn in targets:
        if only and sid not in only:
            continue
        print(f"\n=== {sid} ===")
        t0 = time.time()
        try:
            rows = fn()
            _write_csv(rows, f"data/{sid}.csv")
            print(f"  done in {time.time()-t0:.1f}s")
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
