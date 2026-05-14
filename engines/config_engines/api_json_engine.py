"""
engines/config_engines/api_json_engine.py — POST/GET a JSON API,
navigate to the records array via response_path, and map fields.

Driven by config keys:
  url, method, headers, body_template, response_path, pagination,
  field_map, resilience.

Special-case: when the API returns JSON that wraps an HTML fragment
(e.g. BIS returns {"html": "<table>..."}), set
  "response_format": "html_in_json"
and the engine will parse the table inside the JSON value at
response_path. field_map then uses column-index strings ("0","3")
or header-text strings.
"""

import json
import re
import time
from typing import List

import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

from ._common import (now_ts, schema_record, apply_field_map_dictlike,
                       merged_headers, with_retries, normalise_str)
from .snapshot_manager import save_raw_snapshot


def _call(cfg, url=None, body=None):
    url = url or cfg["url"]
    method = (cfg.get("method") or "GET").upper()
    timeout = int((cfg.get("resilience") or {}).get("timeout_seconds", 60))
    headers = merged_headers(cfg)
    # Default to form-encoded POST unless headers say otherwise.
    use_body = body if body is not None else cfg.get("body_template", {})
    def _do():
        if method == "POST":
            r = requests.post(url, headers=headers, data=use_body,
                               timeout=timeout, verify=False,
                               allow_redirects=True)
        else:
            r = requests.get(url, headers=headers, params=use_body,
                              timeout=timeout, verify=False,
                              allow_redirects=True)
        if r.status_code != 200 or not r.content:
            raise RuntimeError(f"status={r.status_code} len={len(r.content)}")
        return r
    return with_retries(_do, cfg, label=f"{method} {url}")


def _resolve_path(obj, path):
    """Walk a $.a.b.0 style path against `obj`."""
    if not path:
        return obj
    cur = obj
    for part in path.lstrip("$.").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            i = int(part)
            cur = cur[i] if i < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _parse_html_table_value(html_str, cfg):
    """Parse an HTML <table> embedded inside a JSON response."""
    soup = BeautifulSoup(html_str, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return []
    table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = table.find_all("tr")
    headers = []
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
        cell_text = [normalise_str(c.get_text(" ", strip=True))
                      for c in cells]
        if headers and cell_text == headers:
            continue
        if not any(cell_text):
            continue
        d = {}
        for i, t in enumerate(cell_text):
            d[str(i)] = t
            if i < len(headers) and headers[i]:
                d[headers[i]] = t
        out.append(d)
    return out


def _records_from_response(resp, cfg):
    raw = resp.content
    try:
        body = resp.json()
    except Exception:
        return [], raw
    if cfg.get("response_format") == "html_in_json":
        html_str = _resolve_path(body, cfg.get("response_path"))
        if not html_str:
            return [], raw
        return _parse_html_table_value(html_str, cfg), raw
    # Regular path → array of dicts
    records = _resolve_path(body, cfg.get("response_path"))
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        return [], raw
    return records, raw


def _next_pagination(cfg, page_i, response_body, body):
    p = cfg.get("pagination") or {}
    typ = p.get("type", "none")
    if typ in (None, "none"):
        return None
    if typ == "offset":
        size = int(p.get("page_size", 50))
        body = dict(body or {})
        body[p["param"]] = (page_i + 1) * size
        if p.get("size_param"):
            body[p["size_param"]] = size
        return body
    if typ == "page_number":
        body = dict(body or {})
        body[p["param"]] = page_i + 2     # next page (1-indexed by convention)
        return body
    if typ == "cursor":
        cursor = _resolve_path(response_body, p["param"])
        if not cursor:
            return None
        body = dict(body or {})
        body[p.get("set_param", p["param"])] = cursor
        return body
    return None


def run(cfg: dict) -> List[dict]:
    sid = cfg["source_id"]
    fm  = cfg.get("field_map") or {}
    max_pages = int((cfg.get("pagination") or {}).get("max_pages", 50))
    scraped_at = now_ts()
    body = dict(cfg.get("body_template") or {})
    all_records = []
    for page_i in range(max_pages):
        resp = _call(cfg, body=body)
        if (cfg.get("resilience") or {}).get("snapshot_raw", True):
            save_raw_snapshot(sid, resp.content, "json")
        records, _raw = _records_from_response(resp, cfg)
        if not records:
            break
        added = 0
        for raw in records:
            mapped = apply_field_map_dictlike(raw, fm)
            name = normalise_str(mapped.get("name", ""))
            if not name:
                continue
            rec = schema_record(cfg, scraped_at, name=name)
            for k in ("details", "address", "date_of_birth", "document_url",
                      "father_name", "gender", "case_unit",
                      "reward_amount", "interpol_notice_id"):
                if k in mapped and mapped[k] not in (None, ""):
                    rec[k] = normalise_str(mapped[k])
            if rec.get("document_url"):
                rec["has_document"] = "Yes"
            all_records.append(rec)
            added += 1
        if added == 0 or max_pages == 1:
            break
        # Pagination only meaningful if configured
        try:
            full_body = resp.json()
        except Exception:
            full_body = None
        body_next = _next_pagination(cfg, page_i, full_body, body)
        if body_next is None or body_next == body:
            break
        body = body_next
        time.sleep(2.0)
    return all_records
