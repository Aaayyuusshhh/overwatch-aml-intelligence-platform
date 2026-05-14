"""
BSE Arbitration Awards (#237).

Source: https://www.bseindia.com/static/members/list_arbitration_awards

Same Angular-SPA pattern as #160 Defaulter & Expelled Members: the page
is a shell, and the entire table is baked into the JS bundle as static
Angular template instructions inside the
  app-list-arbitration-awards
component.

This scraper reuses the same parsing strategy as
scrapers/bse_defaulter_expelled_members.py:
  1. Fetch the shell, discover main-*.js filename.
  2. Download the bundle, locate the component's consts array and
     template-function body.
  3. Tokenize the template ops, walk them with a tag stack to
     reconstruct rows.

Column structure for Arbitration Awards (confirmed by inspecting the
component's <thead> ops at parse time):
  Sr No | Client Code | Name of Client | Name of Member |
  Date of Award | Public Notice
"""

import csv
import json
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL_URL = "https://www.bseindia.com/static/members/list_arbitration_awards"
BSE_ORIGIN = "https://www.bseindia.com"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "bse_arbitration_awards_237.csv")

# Multiple candidate selectors (Angular component names sometimes change
# between deploys).
CANDIDATE_SELECTORS = (
    '"app-list-arbitration-awards"',
    '"app-arbitration-awards"',
)

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/120.0.0.0 Safari/537.36",
      "Referer": "https://www.bseindia.com/",
      "Accept": "text/html"}


# ---------- shared bundle parser ------------------------------------------
def _fetch_text(url, retries=2):
    last_err = None
    for _ in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=60, verify=False)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.text
            last_err = f"status={r.status_code} len={len(r.content)}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    raise RuntimeError(f"BSE: fetch failed {url}: {last_err}")


def _discover_main_js():
    shell = _fetch_text(SHELL_URL)
    m = re.search(r'src="(/assets/includenew/js/main-[A-Z0-9]+\.js)"', shell)
    if not m:
        raise RuntimeError("BSE: main-*.js not found in shell")
    return urljoin(BSE_ORIGIN, m.group(1))


def _balanced_slice(s, start, opener, closer):
    depth = 1
    i = start
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '"':
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


def _extract_consts_and_template(js, selector):
    comp = js.find(selector)
    if comp < 0:
        return None, None
    ci = js.find("consts:[", comp)
    if ci < 0:
        return None, None
    cstart = ci + len("consts:[")
    _, cend = _balanced_slice(js, cstart, "[", "]")
    consts_js = "[" + js[cstart:cend] + "]"
    consts = json.loads(consts_js)
    tpl = js.find("template:function", cend)
    if tpl < 0:
        return None, None
    tpl_open = js.find("n&1&&(", tpl) + len("n&1&&(")
    _, tpl_end = _balanced_slice(js, tpl_open, "(", ")")
    body = js[tpl_open:tpl_end]
    return consts, body


_OP_RE = re.compile(
    r"""
    (?P<op>[tide])?
    \(
      (?P<idx>\d+)?
      (?:,\s*"(?P<text>(?:[^"\\]|\\.)*)")?
      (?:,\s*(?P<attr>\d+))?
    \)
    """,
    re.X | re.S,
)


def _tokenize(body):
    pos = 0
    n = len(body)
    while pos < n:
        ch = body[pos]
        if ch in " ,\t\n;":
            pos += 1
            continue
        m = _OP_RE.match(body, pos)
        if not m:
            pos += 1
            continue
        op = m.group("op") or "("
        idx = m.group("idx")
        text = m.group("text")
        attr = m.group("attr")
        if text is not None:
            text = text.encode("utf-8").decode("unicode_escape")
        yield (op,
               int(idx) if idx is not None else None,
               text,
               int(attr) if attr is not None else None)
        pos = m.end()


_ATTR_KEYS = {"href", "target", "name", "title", "aria-label", "rowspan",
              "colspan", "align", "valign", "bgcolor", "width", "height"}


def _attrs_to_dict(attr_list):
    out = {}
    i = 0
    while i < len(attr_list):
        v = attr_list[i]
        if isinstance(v, str) and v in _ATTR_KEYS:
            if i + 1 < len(attr_list):
                out[v] = attr_list[i + 1]
            i += 2
        else:
            i += 1
    return out


def _walk(consts, body):
    rows = []
    cur_row = None
    cur_cell = None
    stack = []
    headers = []           # captured from <thead> region
    in_thead = False
    for op, idx, text, attr in _tokenize(body):
        if op in ("t", "("):
            tag = text
            attrs = _attrs_to_dict(consts[attr]) if attr is not None else {}
            stack.append((tag, attrs))
            if tag == "thead":
                in_thead = True
            elif tag == "tbody":
                in_thead = False
            elif tag == "tr":
                cur_row = []
                rows.append(cur_row)
            elif tag == "td" or tag == "th":
                cur_cell = {"text_parts": [], "href": "", "is_header": (tag == "th")}
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
            pass
        elif op == "e":
            if stack:
                stack.pop()
    # Separate header rows (first TR with all <th>) from data rows.
    return rows


def _flatten_cell(c):
    return re.sub(r"\s+", " ", "".join(c["text_parts"])).strip()


def scrape():
    js_url = _discover_main_js()
    print(f"  bundle: {js_url}")
    js = _fetch_text(js_url)
    consts = None
    body = None
    used_selector = None
    for sel in CANDIDATE_SELECTORS:
        consts, body = _extract_consts_and_template(js, sel)
        if consts is not None:
            used_selector = sel
            break
    if consts is None:
        raise RuntimeError(
            f"BSE: none of the candidate selectors found in main.js: "
            f"{CANDIDATE_SELECTORS}"
        )
    print(f"  component: {used_selector}  consts={len(consts)}  "
          f"template={len(body)}")
    rows = _walk(consts, body)
    print(f"  raw rows walked: {len(rows)}")

    # Identify the header row (all <th>) so we can drop it from data.
    headers = []
    data_rows = []
    for r in rows:
        if r and all(c.get("is_header") for c in r):
            if not headers:
                headers = [_flatten_cell(c) for c in r]
            continue
        if not r:
            continue
        # Skip rows whose cells are all empty (table chrome).
        if not any(_flatten_cell(c) for c in r):
            continue
        data_rows.append(r)
    print(f"  headers: {headers}")
    print(f"  data rows: {len(data_rows)}")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for r in data_rows:
        cells = [_flatten_cell(c) for c in r]
        # Pad to header length.
        while len(cells) < len(headers):
            cells.append("")
        sn        = cells[0] if len(cells) > 0 else ""
        client_cd = cells[1] if len(cells) > 1 else ""
        client_nm = cells[2] if len(cells) > 2 else ""
        member_nm = cells[3] if len(cells) > 3 else ""
        date      = cells[4] if len(cells) > 4 else ""
        notice    = cells[5] if len(cells) > 5 else ""
        # Document URL: first non-empty href in the row.
        doc_href = ""
        for c in r:
            if c.get("href"):
                doc_href = urljoin(BSE_ORIGIN, c["href"])
                break
        if not (client_nm or member_nm):
            continue
        # Treat the *member* (the broker who the award was made against)
        # as the primary entity name for screening, and surface the client
        # in details.
        name = member_nm or client_nm
        details = " | ".join(p for p in [
            f"Sr No: {sn}" if sn else "",
            f"Client Code: {client_cd}" if client_cd else "",
            f"Client Name: {client_nm}" if client_nm else "",
            f"Date of Award: {date}" if date else "",
            f"Public Notice: {notice}" if notice else "",
        ] if p)
        out.append({
            "source_agency": "Bombay Stock Exchange (BSE)",
            "source_list": "Arbitration Awards",
            "case_unit": client_cd,
            "name": name,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": "",
            "reward_amount": "",
            "details": details,
            "has_document": "Yes" if doc_href else "No",
            "document_url": doc_href,
            "detail_page_url": SHELL_URL,
            "interpol_notice_id": "",
            "link_kind": "manual_discovery",
            "scraped_at": scraped_at,
            "enrichment_status": "",
        })
    return out


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
    print("BSE Arbitration Awards (#237)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("BSE Arbitration: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
