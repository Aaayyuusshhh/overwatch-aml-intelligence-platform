"""
engines/config_engines/_common.py — shared helpers for the three
config-engine implementations.
"""

import re
import time
from datetime import datetime

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CANONICAL_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalise_str(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def merged_headers(cfg):
    h = dict(CANONICAL_HEADERS)
    h.update(cfg.get("headers") or {})
    return h


def with_retries(callable_, cfg, label="request"):
    """Run `callable_()` with config-driven retry. `callable_` should
    raise on transient failure or return a response on success."""
    r = (cfg.get("resilience") or {})
    attempts = max(1, int(r.get("retries", 3)))
    delay    = int(r.get("retry_delay_seconds", 10))
    last_err = None
    for i in range(attempts):
        try:
            return callable_()
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(delay)
    raise RuntimeError(f"{label}: all {attempts} attempts failed; "
                        f"last error {type(last_err).__name__}: {last_err}")


def schema_record(cfg, scraped_at, name="", **extras):
    """Build a 17-column row dict pre-filled with the agency/list
    fields from the config."""
    base = {
        "source_agency":     cfg.get("agency", ""),
        "source_list":       cfg.get("list_name", ""),
        "case_unit":         "",
        "name":              normalise_str(name),
        "father_name":       "",
        "date_of_birth":     "",
        "gender":            "",
        "address":           "",
        "reward_amount":     "",
        "details":           "",
        "has_document":      "No",
        "document_url":      "",
        "detail_page_url":   cfg.get("url", ""),
        "interpol_notice_id": "",
        "link_kind":         "config_engine",
        "scraped_at":        scraped_at,
        "enrichment_status": "",
    }
    for k, v in extras.items():
        if v is not None:
            base[k] = normalise_str(v) if isinstance(v, str) else v
    return base


def apply_field_map_dictlike(record_obj, field_map):
    """Given a dict-shaped record (CSV row dict or JSON dict) and a
    {schema_field: source_key} field_map, return {schema_field: value}.
    Multi-source-key syntax: "col_a,col_b" joins both with ' | '.
    JSONPath-light syntax: "$.entity.name" navigates nested keys
    (only dot-paths supported)."""
    out = {}
    for schema_field, src_expr in (field_map or {}).items():
        if not isinstance(src_expr, str):
            continue
        # multi-column join
        if "," in src_expr and not src_expr.startswith("$."):
            parts = [p.strip() for p in src_expr.split(",")]
            vals = [_dictlike_get(record_obj, p) for p in parts]
            joined = " | ".join(normalise_str(v) for v in vals
                                  if v not in (None, ""))
            out[schema_field] = joined
        else:
            out[schema_field] = _dictlike_get(record_obj, src_expr)
    return out


def _dictlike_get(obj, expr):
    """Resolve `expr` against a dict-like record. Supports:
      - flat keys "Name"
      - dotted paths "$.entity.name" or "entity.name"
      - integer indices (when obj is a list of cells)
    """
    if obj is None or expr is None:
        return None
    e = expr.strip()
    if e.startswith("$."):
        e = e[2:]
    # try literal key first
    if isinstance(obj, dict) and e in obj:
        return obj[e]
    # integer-index lookup against a list
    if isinstance(obj, (list, tuple)) and e.isdigit():
        i = int(e)
        return obj[i] if i < len(obj) else None
    # dotted path
    if "." in e and isinstance(obj, (dict, list)):
        cur = obj
        for part in e.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit():
                idx = int(part)
                cur = cur[idx] if idx < len(cur) else None
            else:
                return None
            if cur is None:
                return None
        return cur
    return None
