"""
engines/api_replay_handler.py

Generic engine that replays a saved request recipe (utils.request_recipes)
and converts the response into the standard 17-column CSV. Lets us
skip the Playwright tier entirely once the backend API is known.

Recipe schema is defined in utils/request_recipes.py. We extend it with
two optional fields the replay layer cares about:

    "extract_strategy": "json_path" | "json_records" | "html_table" |
                        "html_passthrough" | "raw"
        How to interpret the response body.
            json_path        - extract via dotted JSON path
                              (e.g. "data" or "result.items")
            json_records     - response is a JSON array of dicts
            html_table       - response is HTML; route through the
                              generic html_scraper engine on the saved
                              snippet (NOT a fresh fetch)
            html_passthrough - write the response as a single
                              unstructured row
            raw              - same as html_passthrough but does not
                              try to decode JSON first

    "json_path": "data"
        Dotted path used by extract_strategy=json_path. Optional.

    "field_map":
        Optional mapping of response field names -> our 17-column
        schema column. Example:
            {"company_name": "name", "city": "address",
             "registration_number": "case_unit"}

    "pagination":
        Optional dict for paginated APIs:
            {"param": "page", "start": 1, "limit_param": "size",
             "limit": 50, "max_pages": 100,
             "total_field": "total"}
        Pagination is implemented but rarely needed; most discovered
        Indian regulator APIs return everything in one shot.

Public entry points
-------------------
run(source, recipe_id=None) -> result dict (status / record_count / ...)
    If recipe_id is omitted we look for recipes/<source_id>.json.
    Writes data/<source_id>.csv on success.

replay_to_records(recipe) -> list[dict]
    Lower-level: returns the schema-shaped records without writing
    a CSV. Useful for tests / chaining.
"""

import csv
import json
import os
import re
import time
from datetime import datetime

from utils.request_recipes import load_recipe, replay_recipe

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


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _coerce_str(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:500]
    return str(v)


def _walk_path(obj, dotted_path):
    if not dotted_path:
        return obj
    cur = obj
    for part in dotted_path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _record_from_row(row_dict, source, recipe, scraped_at, link_kind):
    """Map a JSON / HTML-row dict into the 17-column schema."""
    field_map = recipe.get("field_map") or {}

    def pick(*keys):
        for k in keys:
            target = field_map.get(k, k)
            v = row_dict.get(target)
            if v is None:
                # also try case-insensitive lookup
                for rk, rv in row_dict.items():
                    if rk.lower() == target.lower():
                        return _coerce_str(rv).strip()
                continue
            return _coerce_str(v).strip()
        return ""

    name = pick("name", "borrower", "company", "firm",
                "title", "entity")
    if not name or len(name) < 3:
        # Fallback: first non-empty value
        for v in row_dict.values():
            sv = _coerce_str(v).strip()
            if len(sv) >= 4 and not sv.startswith("http"):
                name = sv
                break
    if not name:
        return None

    details = " | ".join(f"{k}: {_coerce_str(v)}" for k, v in row_dict.items()
                         if v is not None and _coerce_str(v).strip())
    return {
        "source_agency":       source["agency"],
        "source_list":         source["list_name"],
        "case_unit":           pick("case_unit", "registration", "id",
                                    "_id", "regn"),
        "name":                name,
        "father_name":         pick("father_name", "father"),
        "date_of_birth":       pick("date_of_birth", "dob"),
        "gender":              pick("gender"),
        "address":             pick("address", "city"),
        "reward_amount":       pick("reward", "amount", "outstanding"),
        "details":             details[:1500],
        "has_document":        "Yes" if row_dict.get("filePath") or
                                       row_dict.get("file") else "No",
        "document_url":        _coerce_str(row_dict.get("filePath")
                                           or row_dict.get("file") or ""),
        "detail_page_url":     source.get("url", ""),
        "interpol_notice_id":  "",
        "link_kind":           link_kind,
        "scraped_at":          scraped_at,
        "enrichment_status":   "none",
    }


def replay_to_records(recipe, source, scraped_at=None):
    scraped_at = scraped_at or _now()
    rid = recipe["recipe_id"]
    strategy = recipe.get("extract_strategy") or "json_records"
    link_kind = recipe.get("link_kind") or "api_replay"

    pagination = recipe.get("pagination")
    pages_to_fetch = [None]  # default single page
    if pagination:
        pages_to_fetch = list(range(
            int(pagination.get("start", 1)),
            int(pagination.get("start", 1)) + int(pagination.get("max_pages", 1))
        ))

    out = []
    for page_idx, page_num in enumerate(pages_to_fetch, start=1):
        if pagination and page_num is not None:
            recipe.setdefault("params", {})
            recipe["params"][pagination["param"]] = page_num
            if pagination.get("limit"):
                recipe["params"][pagination.get("limit_param", "limit")] = \
                    pagination["limit"]
        try:
            resp = replay_recipe(rid, timeout=60)
        except Exception as e:
            print(f"[api_replay] {rid} replay failed: "
                  f"{type(e).__name__}: {e}")
            break
        body = resp.body if hasattr(resp, "body") else resp.content
        if isinstance(body, bytes):
            body = body.decode("utf-8", "ignore")

        records_this_page = []
        if strategy == "raw" or strategy == "html_passthrough":
            records_this_page.append({
                "name": (recipe.get("notes") or rid)[:120],
                "details": body[:1500],
            })
        elif strategy == "html_table":
            # Reuse engines.html_scraper logic without re-fetching.
            try:
                from engines.html_scraper import _parse_largest_table  # type: ignore
            except Exception:
                _parse_largest_table = None
            tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", body, re.I)
            for t in tables:
                trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t, re.I)
                if len(trs) < 2:
                    continue
                header_cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                                for c in re.findall(
                                    r"<t[dh][^>]*>([\s\S]*?)</t[dh]>",
                                    trs[0], re.I)]
                for tr in trs[1:]:
                    cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                             for c in re.findall(
                                 r"<t[dh][^>]*>([\s\S]*?)</t[dh]>",
                                 tr, re.I)]
                    if not any(cells):
                        continue
                    row_dict = dict(zip(header_cells, cells))
                    records_this_page.append(row_dict)
                break
        else:
            # JSON path. extract_strategy in (json_path, json_records).
            try:
                payload = json.loads(body)
            except Exception as e:
                print(f"[api_replay] {rid} not valid JSON: {e}")
                break
            if strategy == "json_records":
                target = payload if isinstance(payload, list) \
                         else _walk_path(payload, recipe.get("json_path", "data"))
            else:
                target = _walk_path(payload, recipe.get("json_path", ""))
            if not isinstance(target, list):
                print(f"[api_replay] {rid} extract path did not yield a list "
                      f"(got {type(target).__name__})")
                break
            for row in target:
                if isinstance(row, dict):
                    records_this_page.append(row)

        for row in records_this_page:
            rec = _record_from_row(row, source, recipe, scraped_at, link_kind)
            if rec:
                out.append(rec)

        # Pagination guard: stop if this page yielded nothing.
        if not records_this_page:
            break

    return out


def _save_csv(rows, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def run(source, recipe_id=None):
    """Standard engine entry point. Returns the framework result dict."""
    start = time.time()
    sid = source["id"]
    rid = recipe_id or sid
    out_path = os.path.join(DATA_DIR, f"{sid}.csv")
    base = {
        "status": "failure", "record_count": 0,
        "runtime_seconds": 0.0, "error": None, "csv_path": None,
        "extraction_strategy": "api_replay",
        "fetch_tier": "api_replay",
    }
    try:
        recipe = load_recipe(rid)
    except FileNotFoundError as e:
        base["error"] = str(e)
        base["runtime_seconds"] = round(time.time() - start, 2)
        return base

    rows = replay_to_records(recipe, source)
    if not rows:
        base["error"] = "recipe replayed but produced 0 records"
        base["runtime_seconds"] = round(time.time() - start, 2)
        return base
    _save_csv(rows, out_path)
    base.update({
        "status": "success",
        "record_count": len(rows),
        "csv_path": out_path,
        "runtime_seconds": round(time.time() - start, 2),
    })
    return base
