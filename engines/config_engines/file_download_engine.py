"""
engines/config_engines/file_download_engine.py — download a file
(CSV/TSV/XML/JSON/XLSX/XLS) and parse it into schema rows.

Driven by config keys:
  url, format, delimiter (csv/tsv), encoding, skip_rows,
  sheet_name (xlsx), response_path (json), field_map, resilience.
"""

import io
import json
import os
import re
import xml.etree.ElementTree as ET
from typing import List

import pandas as pd
import requests
import warnings
warnings.filterwarnings("ignore")

from ._common import (now_ts, schema_record, apply_field_map_dictlike,
                       merged_headers, with_retries, normalise_str)
from .snapshot_manager import save_raw_snapshot


def _fetch_bytes(cfg):
    url = cfg["url"]
    timeout = int((cfg.get("resilience") or {}).get("timeout_seconds", 60))
    def _do():
        r = requests.get(url, headers=merged_headers(cfg),
                          timeout=timeout, verify=False,
                          allow_redirects=True)
        if r.status_code != 200 or not r.content:
            raise RuntimeError(f"status={r.status_code} len={len(r.content)}")
        return r.content
    return with_retries(_do, cfg, label=f"GET {url}")


def _parse_csv_like(content, cfg):
    fmt = cfg["format"]
    delim = cfg.get("delimiter") or ("\t" if fmt == "tsv" else ",")
    encoding = cfg.get("encoding", "utf-8")
    skip = int(cfg.get("skip_rows", 0))
    header = 0 if cfg.get("has_header", True) else None
    text = content.decode(encoding, errors="replace")
    df = pd.read_csv(io.StringIO(text), delimiter=delim,
                      skiprows=skip, dtype=str, keep_default_na=False,
                      header=header)
    if header is None:
        # Column names become "0","1","2" so field_map can use indices.
        df.columns = [str(i) for i in range(len(df.columns))]
    else:
        df.columns = [str(c).strip() for c in df.columns]
    return df.to_dict("records")


def _parse_excel(content, cfg):
    fmt = cfg["format"]
    skip = int(cfg.get("skip_rows", 0))
    sheet = cfg.get("sheet_name", 0)
    header = 0 if cfg.get("has_header", True) else None
    engine = "openpyxl" if fmt == "xlsx" else "xlrd"
    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name=sheet,
                            engine=engine, skiprows=skip, dtype=str,
                            keep_default_na=False, header=header)
    except Exception:
        # Some "xls" files are HTML/TSV; let csv_like try
        return _parse_csv_like(content, dict(cfg, format="tsv"))
    if header is None:
        df.columns = [str(i) for i in range(len(df.columns))]
    else:
        df.columns = [str(c).strip() for c in df.columns]
    return df.to_dict("records")


def _parse_xml(content, cfg):
    """XML: response_path is an XPath-ish row selector (e.g.
    './/record'). field_map values are tag names or sub-XPaths."""
    root = ET.fromstring(content)
    row_path = cfg.get("response_path") or ".//*"
    rows = []
    for el in root.iterfind(row_path):
        d = {child.tag: (child.text or "").strip()
              for child in el}
        if not d:
            continue
        rows.append(d)
    return rows


def _parse_json(content, cfg):
    body = json.loads(content.decode(
        cfg.get("encoding", "utf-8"), errors="replace"))
    rp = cfg.get("response_path")
    if rp:
        cur = body
        for part in rp.lstrip("$.").split("."):
            if not part:
                continue
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit():
                cur = cur[int(part)]
            if cur is None:
                break
        body = cur
    if isinstance(body, dict):
        body = [body]
    if not isinstance(body, list):
        return []
    return body


_PARSERS = {
    "csv":  _parse_csv_like,
    "tsv":  _parse_csv_like,
    "xlsx": _parse_excel,
    "xls":  _parse_excel,
    "xml":  _parse_xml,
    "json": _parse_json,
}


def run(cfg: dict) -> List[dict]:
    sid = cfg["source_id"]
    fmt = cfg["format"]
    content = _fetch_bytes(cfg)
    if (cfg.get("resilience") or {}).get("snapshot_raw", True):
        save_raw_snapshot(sid, content, fmt)
    raw_rows = _PARSERS[fmt](content, cfg)
    scraped_at = now_ts()
    out = []
    fm = cfg.get("field_map") or {}
    for raw in raw_rows:
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
        out.append(rec)
    return out
