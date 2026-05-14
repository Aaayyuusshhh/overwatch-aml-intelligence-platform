"""
engines/config_engines/html_table_engine.py — fetch an HTML page,
locate a table, and extract rows according to field_map.

Driven by config keys:
  url, table_selector, row_selector, pagination, field_map, resilience.

field_map values are either:
  • column header text  ("Name of Firm")
  • column index as string ("0", "3")
  • comma-separated list of either, joined with " | " in details
"""

import re
import time
from typing import List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

from ._common import (now_ts, schema_record, apply_field_map_dictlike,
                       merged_headers, with_retries, normalise_str)
from .snapshot_manager import save_raw_snapshot


def _fetch_html(url, cfg):
    timeout = int((cfg.get("resilience") or {}).get("timeout_seconds", 60))
    def _do():
        r = requests.get(url, headers=merged_headers(cfg),
                          timeout=timeout, verify=False,
                          allow_redirects=True)
        if r.status_code != 200 or len(r.content) < 200:
            raise RuntimeError(f"status={r.status_code} len={len(r.content)}")
        return r
    return with_retries(_do, cfg, label=f"GET {url}")


def _pick_table(soup, selector):
    if selector:
        tables = soup.select(selector)
    else:
        tables = soup.find_all("table")
    if not tables:
        return None
    # If multiple tables, pick the one with the most rows (avoids
    # navigation/template tables).
    return max(tables, key=lambda t: len(t.find_all("tr")))


def _parse_table(html, cfg):
    """Yield dict-shaped rows: each row is {header_text: cell_text}
    plus integer-keyed cells {0: ..., 1: ...} so field_map can use
    either."""
    soup = BeautifulSoup(html, "html.parser")
    table = _pick_table(soup, cfg.get("table_selector"))
    if table is None:
        return [], [], None
    rows = table.find_all(cfg.get("row_selector", "tr"))
    headers = []
    # First row with <th> tags is the header; fall back to the first row.
    for tr in rows:
        ths = tr.find_all("th")
        if ths:
            headers = [normalise_str(th.get_text(" ", strip=True))
                        for th in ths]
            break
    out = []
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        # Skip the header row by content
        cell_text = [normalise_str(c.get_text(" ", strip=True))
                     for c in cells]
        if headers and cell_text == headers:
            continue
        if not any(cell_text):
            continue
        d = {}
        for i, txt in enumerate(cell_text):
            d[str(i)] = txt
            if i < len(headers) and headers[i]:
                d[headers[i]] = txt
        # Surface anchor href on the row (first <a> wins) so field_map
        # can use a literal "_href" key for document_url.
        anchors = [a for c in cells for a in c.find_all("a", href=True)]
        if anchors:
            d["_href"] = anchors[0]["href"]
        out.append(d)
    return out, headers, table


def _next_pagination_url(cfg, current_url, page_index, html):
    p = cfg.get("pagination") or {}
    typ = p.get("type", "none")
    if typ in (None, "none"):
        return None
    if typ == "query_param":
        param = p["param"]
        step = int(p.get("step", 1))
        start = int(p.get("start", 0))
        nxt = start + step * (page_index + 1)
        sep = "&" if "?" in current_url else "?"
        # Strip any existing param of same name from current_url first.
        cleaned = re.sub(rf"([?&]){re.escape(param)}=[^&]*", r"\1", current_url)
        cleaned = cleaned.rstrip("?&")
        sep = "&" if "?" in cleaned else "?"
        return f"{cleaned}{sep}{param}={nxt}"
    if typ == "next_link":
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            txt = normalise_str(a.get_text(" ", strip=True)).lower()
            if txt in ("next", "next »", "›", "» next") or "next" in txt.split():
                return urljoin(current_url, a["href"])
        return None
    if typ == "offset":
        param = p["param"]
        size = int(p.get("page_size", 50))
        nxt_offset = (page_index + 1) * size
        cleaned = re.sub(rf"([?&]){re.escape(param)}=[^&]*", r"\1", current_url)
        cleaned = cleaned.rstrip("?&")
        sep = "&" if "?" in cleaned else "?"
        return f"{cleaned}{sep}{param}={nxt_offset}"
    return None


def run(cfg: dict) -> List[dict]:
    sid = cfg["source_id"]
    url = cfg["url"]
    fm  = cfg.get("field_map") or {}
    max_pages = int((cfg.get("pagination") or {}).get("max_pages", 50))
    scraped_at = now_ts()
    seen_keys = set()
    all_records = []
    for page_i in range(max_pages):
        r = _fetch_html(url, cfg)
        if (cfg.get("resilience") or {}).get("snapshot_raw", True):
            save_raw_snapshot(sid, r.content, "html")
        rows, headers, _t = _parse_table(r.text, cfg)
        if not rows:
            break
        added = 0
        for raw in rows:
            mapped = apply_field_map_dictlike(raw, fm)
            name = normalise_str(mapped.get("name", ""))
            if not name:
                continue
            key = (name.lower(),
                   normalise_str(mapped.get("details", ""))[:120])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rec = schema_record(cfg, scraped_at, name=name)
            for k in ("details", "address", "date_of_birth", "document_url",
                      "father_name", "gender", "case_unit",
                      "reward_amount", "interpol_notice_id"):
                if k in mapped and mapped[k] not in (None, ""):
                    rec[k] = normalise_str(mapped[k])
            href = raw.get("_href")
            if href and not rec.get("document_url"):
                rec["document_url"] = urljoin(url, href)
            if rec.get("document_url"):
                rec["has_document"] = "Yes"
            all_records.append(rec)
            added += 1
        if added == 0:
            break
        nxt = _next_pagination_url(cfg, url, page_i, r.text)
        if not nxt or nxt == url:
            break
        url = nxt
        time.sleep(2.0)
    return all_records
