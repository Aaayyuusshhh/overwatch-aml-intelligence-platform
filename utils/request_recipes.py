"""
utils/request_recipes.py

Persist and replay HTTP "recipes" — the bare-metal API call (URL, method,
headers, params, body, cookies) that a scraper discovered through
DevTools / Playwright network capture. Once a recipe exists, future
runs can call the API directly via Scrapling without spinning up a
browser. This is critical for JS-walled portals (SEBI, BSE, NSE, MCA)
where browser rendering is the only way to find the API the first
time, but every subsequent run can hit the API directly.

Recipe schema (one JSON file per recipe under recipes/<recipe_id>.json):

    {
        "recipe_id":      "cvc_get_all_vigilence",
        "source_id":      "cvc_penalties_for_prosecution_16",  // optional
        "url":            "https://cvc.gov.in/api/get-all-vigilence",
        "method":         "GET",
        "headers":        {...},
        "params":         {...},          // query-string args
        "body":           null,           // for POST/PUT
        "cookies":        {...},
        "discovered_at":  "2026-05-08",
        "response_type":  "json",
        "notes":          "..."
    }

Public functions
----------------
save_recipe(recipe_id, request_data)  -> path
replay_recipe(recipe_id)              -> Scrapling response object
list_recipes()                        -> list of (recipe_id, url, source_id)
delete_recipe(recipe_id)
load_recipe(recipe_id)                -> dict
"""

import json
import os
from datetime import datetime

from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPES_DIR = os.path.join(PROJECT_ROOT, "recipes")

REQUIRED_FIELDS = ("url", "method")


def _path(recipe_id):
    return os.path.join(RECIPES_DIR, f"{recipe_id}.json")


def save_recipe(recipe_id, request_data):
    """Validate + persist a recipe. recipe_id should be a short stable
    identifier. Stamps discovered_at with today's date if absent."""
    if not all(k in request_data for k in REQUIRED_FIELDS):
        raise ValueError(f"recipe missing required fields {REQUIRED_FIELDS}")
    os.makedirs(RECIPES_DIR, exist_ok=True)
    payload = {"recipe_id": recipe_id, **request_data}
    payload.setdefault("discovered_at",
                       datetime.now().strftime("%Y-%m-%d"))
    payload.setdefault("method", "GET")
    payload.setdefault("headers", {})
    payload.setdefault("params", {})
    payload.setdefault("body", None)
    payload.setdefault("cookies", {})
    payload.setdefault("response_type", "json")
    payload.setdefault("notes", "")
    out = _path(recipe_id)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[request_recipes] saved {recipe_id} -> {out}")
    return out


def load_recipe(recipe_id):
    p = _path(recipe_id)
    if not os.path.exists(p):
        raise FileNotFoundError(f"no recipe at {p}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def replay_recipe(recipe_id, timeout=30):
    """Execute the saved request via Scrapling Fetcher. Returns the
    Scrapling response object. Raises if the recipe doesn't exist."""
    rec = load_recipe(recipe_id)
    url = rec["url"]
    method = (rec.get("method") or "GET").upper()
    params = rec.get("params") or {}
    headers = rec.get("headers") or {}

    if method == "GET":
        # Scrapling Fetcher.get accepts headers via a dict
        return Fetcher.get(url, timeout=timeout, retries=1, retry_delay=0,
                           verify=False, params=params, headers=headers)
    elif method == "POST":
        return Fetcher.post(url, timeout=timeout, retries=1, retry_delay=0,
                            verify=False,
                            data=rec.get("body"),
                            headers=headers,
                            params=params)
    else:
        raise NotImplementedError(f"recipe method {method} not yet supported")


def list_recipes():
    """Return [(recipe_id, url, source_id), ...] sorted by recipe_id."""
    if not os.path.isdir(RECIPES_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(RECIPES_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(RECIPES_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        out.append((d.get("recipe_id", fn[:-5]), d.get("url", ""),
                    d.get("source_id", "")))
    return out


def delete_recipe(recipe_id):
    p = _path(recipe_id)
    if os.path.exists(p):
        os.remove(p)
        print(f"[request_recipes] deleted {p}")


if __name__ == "__main__":
    recs = list_recipes()
    print(f"Saved recipes ({len(recs)}):")
    for rid, url, sid in recs:
        print(f"  {rid}  -> {url}  (source: {sid or '-'})")
