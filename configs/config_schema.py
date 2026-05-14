"""
configs/config_schema.py — schema for the config-driven engine.

Every JSON file under configs/sources/ that drives the new
`config` handler must satisfy this schema. The validator is
deliberately lightweight: it reports actionable errors, not
exhaustive type coercion, so authors can iterate on a config and
get clear feedback on what's wrong.

  ok, errors = validate_config(cfg)

Returns:
  ok: bool
  errors: list[str]   (empty when ok is True)
"""

from typing import Any, Tuple, List

ENGINES = ("file_download", "html_table", "api_json")
FILE_FORMATS = ("csv", "tsv", "xml", "json", "xlsx", "xls")
HTTP_METHODS = ("GET", "POST")

# Defaults applied at validate time so consumers can pass `cfg["resilience"]["retries"]`
# without checking presence.
DEFAULT_VALIDATION = {
    "min_records": 1,
    "required_fields": ["name"],
    "max_empty_name_pct": 5.0,
}
DEFAULT_RESILIENCE = {
    "retries": 3,
    "retry_delay_seconds": 10,
    "timeout_seconds": 60,
    "snapshot_raw": True,
}


# ---------- helpers --------------------------------------------------------
def _is_str(v):     return isinstance(v, str) and v
def _is_int(v):     return isinstance(v, int) and not isinstance(v, bool)
def _is_num(v):     return isinstance(v, (int, float)) and not isinstance(v, bool)
def _is_dict(v):    return isinstance(v, dict)
def _is_list(v):    return isinstance(v, list)


def _check(condition, errors, message):
    if not condition:
        errors.append(message)


def _apply_defaults(cfg):
    """Mutate `cfg` in place: fill validation/resilience defaults."""
    v = cfg.setdefault("validation", {})
    for k, default in DEFAULT_VALIDATION.items():
        v.setdefault(k, default)
    r = cfg.setdefault("resilience", {})
    for k, default in DEFAULT_RESILIENCE.items():
        r.setdefault(k, default)


# ---------- per-engine validators ------------------------------------------
def _validate_file_download(cfg, errors):
    _check(cfg.get("format") in FILE_FORMATS, errors,
           f'file_download: "format" must be one of {FILE_FORMATS}')
    if cfg.get("format") in ("csv", "tsv"):
        # delimiter is optional (defaults from format)
        delim = cfg.get("delimiter")
        if delim is not None and not _is_str(delim):
            errors.append('file_download: "delimiter" must be a string')
    skip = cfg.get("skip_rows", 0)
    _check(_is_int(skip) and skip >= 0, errors,
           '"skip_rows" must be a non-negative integer')
    enc = cfg.get("encoding", "utf-8")
    _check(_is_str(enc), errors, '"encoding" must be a string')


def _validate_html_table(cfg, errors):
    sel = cfg.get("table_selector")
    if sel is not None:
        _check(_is_str(sel), errors, '"table_selector" must be a string')
    if "pagination" in cfg:
        _validate_pagination(cfg["pagination"], errors,
                              allowed={"query_param", "next_link",
                                       "offset", "none"})


def _validate_api_json(cfg, errors):
    _check(cfg.get("method", "GET") in HTTP_METHODS, errors,
           f'api_json: "method" must be one of {HTTP_METHODS}')
    headers = cfg.get("headers", {})
    _check(_is_dict(headers), errors, '"headers" must be a dict')
    body = cfg.get("body_template")
    if body is not None:
        _check(_is_dict(body), errors, '"body_template" must be a dict')
    rp = cfg.get("response_path")
    if rp is not None:
        _check(_is_str(rp), errors, '"response_path" must be a string')
    if "pagination" in cfg:
        _validate_pagination(cfg["pagination"], errors,
                              allowed={"offset", "cursor",
                                       "page_number", "none"})


def _validate_pagination(p, errors, allowed):
    _check(_is_dict(p), errors, '"pagination" must be a dict')
    if not _is_dict(p):
        return
    typ = p.get("type")
    _check(typ in allowed, errors,
           f'"pagination.type" must be one of {sorted(allowed)}')
    max_pages = p.get("max_pages", 50)
    _check(_is_int(max_pages) and max_pages > 0, errors,
           '"pagination.max_pages" must be a positive integer')


# ---------- main ----------------------------------------------------------
def validate_config(cfg: dict) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    if not _is_dict(cfg):
        return False, ["config must be a JSON object"]

    # Top-level required strings
    for key in ("source_id", "agency", "list_name", "country",
                 "engine", "url"):
        _check(_is_str(cfg.get(key)), errors,
               f'top-level "{key}" must be a non-empty string')

    if cfg.get("engine") and cfg["engine"] not in ENGINES:
        errors.append(f'"engine" must be one of {ENGINES}')

    fm = cfg.get("field_map")
    _check(_is_dict(fm), errors, '"field_map" must be a dict')
    if _is_dict(fm):
        _check(_is_str(fm.get("name")), errors,
               '"field_map.name" must be a non-empty string')

    # Apply defaults (safe to call on validated config)
    _apply_defaults(cfg)

    v = cfg["validation"]
    _check(_is_int(v.get("min_records")) and v["min_records"] >= 0,
           errors, '"validation.min_records" must be a non-negative integer')
    _check(_is_list(v.get("required_fields"))
           and all(_is_str(x) for x in v["required_fields"]), errors,
           '"validation.required_fields" must be a list of strings')
    _check(_is_num(v.get("max_empty_name_pct"))
           and 0 <= v["max_empty_name_pct"] <= 100, errors,
           '"validation.max_empty_name_pct" must be 0-100')

    r = cfg["resilience"]
    _check(_is_int(r["retries"]) and r["retries"] >= 0, errors,
           '"resilience.retries" must be a non-negative integer')
    _check(_is_int(r["retry_delay_seconds"]) and r["retry_delay_seconds"] >= 0,
           errors,
           '"resilience.retry_delay_seconds" must be a non-negative integer')
    _check(_is_int(r["timeout_seconds"]) and r["timeout_seconds"] > 0, errors,
           '"resilience.timeout_seconds" must be a positive integer')
    _check(isinstance(r["snapshot_raw"], bool), errors,
           '"resilience.snapshot_raw" must be a boolean')

    # Engine-specific
    if cfg.get("engine") == "file_download":
        _validate_file_download(cfg, errors)
    elif cfg.get("engine") == "html_table":
        _validate_html_table(cfg, errors)
    elif cfg.get("engine") == "api_json":
        _validate_api_json(cfg, errors)

    return (len(errors) == 0), errors
