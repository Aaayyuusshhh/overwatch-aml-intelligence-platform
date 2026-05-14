"""
BSE Defaulter & Expelled Members (#160).

Source: https://www.bseindia.com/static/members/list_defaulters_expelled_members

The page is the Angular SPA shell. The actual table content is *baked
into the JS bundle* main-*.js as static Angular template instructions
inside the component
  selectors: [["app-list-defaulters-expelled-members"]]

There is no XHR endpoint behind this page; the data lives in the
bundle. This scraper:

  1. Fetches the SPA shell to discover the current main-*.js URL.
  2. Downloads main-*.js (~15 MB).
  3. Locates the defaulters component and extracts
       - `consts:` — the array of attribute arrays referenced by
         template ops by index, and
       - the template function body — the sequence of element ops
         (t = open, i = inner text, d = void, e = close).
  4. Walks the ops sequentially. Whenever a <tr> opens we start a new
     row; <td> opens start a new cell; <a href> inside a cell stashes
     the URL; <i> font icons inside <td> are noted as a PDF marker.
  5. Groups rows by Clg.No.: each "member row" starts a new record
     and subsequent director-only rows belong to the same member.

Output: data/bse_defaulter_expelled_members_160.csv (17-column schema).
"""

import csv
import json
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL_URL = "https://www.bseindia.com/static/members/list_defaulters_expelled_members"
OUTPUT_FILE = os.path.join(
    PROJECT_ROOT, "data", "bse_defaulter_expelled_members_160.csv"
)
BSE_ORIGIN = "https://www.bseindia.com"
COMPONENT_SELECTOR = '"app-list-defaulters-expelled-members"'

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ----- bundle discovery ----------------------------------------------------
def _fetch_text(url):
    r = requests.get(url, headers=UA, timeout=60, verify=False)
    if r.status_code != 200 or len(r.content) < 1000:
        raise RuntimeError(f"BSE: failed to fetch {url} (status={r.status_code})")
    return r.text


def _discover_main_js():
    shell = _fetch_text(SHELL_URL)
    m = re.search(r'src="(/assets/includenew/js/main-[A-Z0-9]+\.js)"', shell)
    if not m:
        raise RuntimeError("BSE: could not find main-*.js path in shell")
    return urljoin(BSE_ORIGIN, m.group(1))


# ----- extraction from the bundle ------------------------------------------
def _balanced_slice(s, start, opener, closer):
    """Return the slice [start, end) containing a balanced opener/closer
    span. start must point just *past* the opening token. Skips over
    string literals."""
    depth = 1
    i = start
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '"':
            # skip string literal
            j = i + 1
            while j < n:
                if s[j] == "\\":
                    j += 2
                    continue
                if s[j] == '"':
                    break
                j += 1
            i = j + 1
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return start, i
        i += 1
    raise RuntimeError("unterminated balanced slice")


def _extract_consts_and_template(js):
    comp = js.find(COMPONENT_SELECTOR)
    if comp < 0:
        raise RuntimeError(f"BSE: component {COMPONENT_SELECTOR} not in main.js")
    ci = js.find("consts:[", comp)
    cstart = ci + len("consts:[")
    _, cend = _balanced_slice(js, cstart, "[", "]")
    consts_js = "[" + js[cstart:cend] + "]"
    consts = json.loads(consts_js)

    tpl = js.find("template:function", cend)
    tpl_open = js.find("n&1&&(", tpl) + len("n&1&&(")
    _, tpl_end = _balanced_slice(js, tpl_open, "(", ")")
    body = js[tpl_open:tpl_end]
    return consts, body


# ----- template-body op tokenizer ------------------------------------------
_OP_RE = re.compile(
    r"""
    (?P<op>[tide])?           # operation letter; missing means chained '('
    \(
      (?P<idx>\d+)?           # leading integer index
      (?:,\s*"(?P<text>(?:[^"\\]|\\.)*)")?   # quoted string arg
      (?:,\s*(?P<attr>\d+))?  # numeric attr-array index
    \)
    """,
    re.X | re.S,
)


def _tokenize(body):
    """Yield (op_letter, idx, text, attr) tuples in document order.
    op_letter is one of 't','i','d','e','('. The '(' op represents a
    chained-open after a previous t(...). They behave like 't' opens."""
    pos = 0
    n = len(body)
    while pos < n:
        ch = body[pos]
        if ch in " ,\t\n;":
            pos += 1
            continue
        m = _OP_RE.match(body, pos)
        if not m:
            # Skip any unrelated chars (closures like &&, !, etc.)
            pos += 1
            continue
        op = m.group("op") or "("
        idx = m.group("idx")
        text = m.group("text")
        attr = m.group("attr")
        if text is not None:
            # Unescape JS string
            text = text.encode("utf-8").decode("unicode_escape")
        yield (
            op,
            int(idx) if idx is not None else None,
            text,
            int(attr) if attr is not None else None,
        )
        pos = m.end()


# ----- walk ops -> rows ----------------------------------------------------
def _attrs_to_dict(attr_list):
    """consts entries are mixed: ["href","/url","target","_blank",1,"cls"].
    Convert to a dict of recognised attributes (href / class / etc.)."""
    out = {}
    i = 0
    while i < len(attr_list):
        v = attr_list[i]
        if isinstance(v, str) and v in {"href", "target", "name", "title",
                                         "aria-label", "rowspan", "colspan",
                                         "align", "valign", "bgcolor",
                                         "width", "height"}:
            if i + 1 < len(attr_list):
                out[v] = attr_list[i + 1]
            i += 2
        else:
            i += 1
    return out


def _walk(consts, body):
    """Walk template ops and return list of rows, where each row is a
    list of cells, and each cell is dict with text / href / has_pdf."""
    rows = []
    cur_row = None
    cur_cell = None
    # Stack of open tag names so e() knows what we're closing.
    stack = []
    for op, idx, text, attr in _tokenize(body):
        if op == "t" or op == "(":
            tag = text
            attrs = _attrs_to_dict(consts[attr]) if attr is not None else {}
            stack.append((tag, attrs))
            if tag == "tr":
                cur_row = []
                rows.append(cur_row)
            elif tag == "td":
                cur_cell = {"text_parts": [], "href": "", "has_pdf": False}
                if cur_row is not None:
                    cur_row.append(cur_cell)
            elif tag == "a" and cur_cell is not None:
                href = attrs.get("href", "")
                if href:
                    cur_cell["href"] = href
        elif op == "i":
            if cur_cell is not None and text:
                cur_cell["text_parts"].append(text)
        elif op == "d":
            tag = text
            attrs = _attrs_to_dict(consts[attr]) if attr is not None else {}
            if cur_cell is not None and tag == "i":
                cur_cell["has_pdf"] = True
        elif op == "e":
            if stack:
                stack.pop()
            if not stack or stack[-1][0] != "td":
                # if we just popped a td, finalise the cell
                pass
    return rows


# ----- group cells into one record per member ------------------------------
def _flatten_cell(c):
    return re.sub(r"\s+", " ", "".join(c["text_parts"])).strip()


def _is_member_row(cells):
    """A 'member row' has 10 cells AND a non-empty Clg.No. cell."""
    if len(cells) != 10:
        return False
    clg = _flatten_cell(cells[0])
    return bool(clg) and clg != "-"


def _build_records(rows, scraped_at):
    out = []
    # Walk rows; member rows start a new record, subsequent rows with
    # 1 cell are director continuations and append to the previous one.
    cur = None
    for row in rows:
        cells = row
        if _is_member_row(cells):
            if cur is not None:
                out.append(_finalise(cur, scraped_at))
            cur = {
                "clg_no": _flatten_cell(cells[0]),
                "name": _flatten_cell(cells[1]),
                "directors": [_flatten_cell(cells[2])] if _flatten_cell(cells[2]) else [],
                "dates": [_flatten_cell(cells[3])] if _flatten_cell(cells[3]) else [],
                "remarks": [_flatten_cell(cells[4])] if _flatten_cell(cells[4]) else [],
                "exchange_notice": _flatten_cell(cells[5]),
                "public_notice_href": cells[6].get("href", ""),
                "exchange_notice_href": cells[5].get("href", ""),
                "order_copy_href": cells[7].get("href", ""),
                "sebi_show_cause_href": cells[8].get("href", ""),
                "ebi_order_href": cells[9].get("href", ""),
            }
        elif cur is not None and len(cells) == 1:
            # Director continuation row.
            extra = _flatten_cell(cells[0])
            if extra and extra not in cur["directors"]:
                cur["directors"].append(extra)
    if cur is not None:
        out.append(_finalise(cur, scraped_at))
    return out


def _finalise(r, scraped_at):
    # Build the details string per spec.
    detail_parts = [f"Clg No: {r['clg_no']}"] if r["clg_no"] else []
    if r["remarks"] and r["dates"]:
        status_parts = []
        for rem, dt in zip(r["remarks"], r["dates"]):
            if rem and dt:
                status_parts.append(f"{rem} ({dt})")
            elif rem:
                status_parts.append(rem)
        if status_parts:
            detail_parts.append("Status: " + ", ".join(status_parts))
    elif r["remarks"]:
        detail_parts.append("Status: " + " | ".join(r["remarks"]))
    directors = [d for d in r["directors"] if d and d != "-"]
    if directors:
        detail_parts.append("Directors: " + ", ".join(directors))
    if r["exchange_notice"]:
        detail_parts.append(f"Exchange Notice: {r['exchange_notice']}")

    document_url = ""
    has_document = "No"
    for k in ("public_notice_href", "exchange_notice_href", "order_copy_href",
              "sebi_show_cause_href", "ebi_order_href"):
        if r.get(k):
            document_url = urljoin(BSE_ORIGIN, r[k])
            has_document = "Yes"
            break

    return {
        "source_agency": "Bombay Stock Exchange (BSE)",
        "source_list": "Defaulter and Expelled Members",
        "case_unit": r["clg_no"],
        "name": r["name"],
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": " | ".join(detail_parts),
        "has_document": has_document,
        "document_url": document_url,
        "detail_page_url": SHELL_URL,
        "interpol_notice_id": "",
        "link_kind": "manual_discovery",
        "scraped_at": scraped_at,
        "enrichment_status": "",
    }


# ----- entry --------------------------------------------------------------
def scrape():
    js_url = _discover_main_js()
    print(f"  main bundle: {js_url}")
    js = _fetch_text(js_url)
    consts, body = _extract_consts_and_template(js)
    print(f"  consts={len(consts)}  template body bytes={len(body):,}")
    rows = _walk(consts, body)
    print(f"  walked rows: {len(rows)}")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recs = _build_records(rows, scraped_at)
    return recs


def save_to_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {path}")


def run():
    print("=" * 60)
    print("BSE Defaulter & Expelled Members (#160)")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
