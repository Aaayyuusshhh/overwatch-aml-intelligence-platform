"""
engines/aspnet_handler.py

Generic ASP.NET form handler for Playwright. Indian govt portals such
as fcraonline.nic.in, several state .nic.in / .gov.in sites, and the
older RBI / DGFT services use the classic Web Forms postback model:

  GET  page.aspx                 -> hidden __VIEWSTATE / __EVENTVALIDATION
                                    + dropdowns + submit button
  POST page.aspx (same URL)      -> server returns the same page, now
                                    populated with the result table

The session cookie issued on the first GET is required for the POST,
which is why static-fetch replay (Scrapling) typically fails: it gets a
fresh session per request and the server rejects the implicit
viewstate handshake. Driving the form inside Playwright keeps cookies
+ viewstate consistent and Just Works.

This module exposes thin helpers around that pattern. It is NOT a
scraper; it provides the building blocks (form-state read,
dropdown-iterate, submit-and-extract) that source-specific scrapers
compose. FCRA (#71) is the proof-of-concept user.

Public API
----------
get_form_state(page) -> dict
    Returns every <input type=hidden> as a name->value map. For an
    ASP.NET page this includes __VIEWSTATE / __EVENTVALIDATION / etc.
    Useful for asserting that the page is ASP.NET and for snapshotting
    a starting state before postbacks.

list_dropdowns(page) -> list[dict]
    Returns [{name, options:[(value,text), ...]}]. Lets a scraper
    discover what dropdowns exist before deciding how to drive them.

select_option(page, field_name, value, settle_ms=600)
    page.select_option wrapper with a small settle delay. Some ASP.NET
    pages do an auto-postback on change; the delay lets that complete.

submit_form(page, button_selector, wait_after_ms=800,
            networkidle_timeout_ms=45_000)
    Click + wait_for_load_state networkidle, then a fixed settle.

extract_table_after_submit(page, predicate=None, header_must_contain=None)
    Re-reads page.content() and returns (headers, rows) for the
    largest table that satisfies the predicate. Either pass a custom
    predicate or a tuple of header keywords (lowercase substrings, all
    must appear in the joined headers).

iterate_dropdown(page, dropdown_name, submit_selector,
                 reset_url=None, header_must_contain=None,
                 between_iterations_ms=800)
    Generator yielding (option_value, option_text, headers, rows) for
    every non-empty option in the named dropdown. Resets the form by
    going back / reloading reset_url between iterations.

Conventions
-----------
- Caller provides the Playwright page (and is responsible for opening
  / closing the browser and context). This keeps the module decoupled
  from utils/browser_profiles, which the caller may or may not use.
- All helpers fail loud with descriptive RuntimeErrors so a bad
  selector doesn't silently produce empty CSVs.
"""

import re

ASPNET_HIDDEN_FIELDS = (
    "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
    "__EVENTTARGET", "__EVENTARGUMENT",
)


def _clean(s):
    if s is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", s).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def get_form_state(page):
    """Return every hidden <input> name -> value on the current page."""
    out = {}
    for el in page.query_selector_all("input[type=hidden]"):
        name = el.get_attribute("name") or ""
        if not name:
            continue
        out[name] = el.get_attribute("value") or ""
    return out


def list_dropdowns(page):
    """Return one dict per <select>: {name, options}."""
    out = []
    for sel in page.query_selector_all("select"):
        name = sel.get_attribute("name") or sel.get_attribute("id") or ""
        if not name:
            continue
        opts = []
        for o in sel.query_selector_all("option"):
            opts.append((o.get_attribute("value") or "",
                         (o.inner_text() or "").strip()))
        out.append({"name": name, "options": opts})
    return out


def select_option(page, field_name, value, settle_ms=600):
    page.select_option(f"select[name='{field_name}']", value=value,
                       timeout=20_000)
    page.wait_for_timeout(settle_ms)


def submit_form(page, button_selector,
                wait_after_ms=800, networkidle_timeout_ms=45_000):
    page.click(button_selector, timeout=20_000)
    try:
        page.wait_for_load_state("networkidle",
                                 timeout=networkidle_timeout_ms)
    except Exception:
        # Some forms never reach networkidle (long-poll keepalives etc.).
        # Settle delay below still gives the table time to render.
        pass
    page.wait_for_timeout(wait_after_ms)


def _parse_html_tables(html):
    """Return [(headers, rows), ...] for every <table> with >= 2 rows."""
    out = []
    for tt in re.findall(r"<table[^>]*>([\s\S]*?)</table>", html, re.I):
        trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", tt, re.I)
        if len(trs) < 2:
            continue
        rows = [[_clean(c) for c in re.findall(
                    r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)]
                for tr in trs]
        if any(any(c for c in r) for r in rows):
            out.append((rows[0], rows[1:]))
    return out


def extract_table_after_submit(page, predicate=None, header_must_contain=None):
    """Pick the largest matching table on the rendered page.

    `predicate(headers, rows) -> bool` wins if provided.
    Else, if `header_must_contain` is a tuple, all those lowercase
    substrings must appear in the joined headers.
    Else: largest table by row count is returned.
    Returns (headers, rows) or (None, []) if no table matches.
    """
    html = page.content()
    candidates = _parse_html_tables(html)
    if not candidates:
        return None, []

    if predicate is not None:
        winners = [(h, r) for h, r in candidates if predicate(h, r)]
    elif header_must_contain:
        keys = tuple(k.lower() for k in header_must_contain)
        winners = []
        for h, r in candidates:
            joined = " ".join(c.lower() for c in h)
            if all(k in joined for k in keys):
                winners.append((h, r))
    else:
        winners = candidates

    if not winners:
        return None, []
    # Largest by row count.
    winners.sort(key=lambda t: -len(t[1]))
    return winners[0]


def iterate_dropdown(page, dropdown_name, submit_selector,
                     reset_url=None, header_must_contain=None,
                     between_iterations_ms=800,
                     skip_values=("",)):
    """Iterate every option in `dropdown_name`, submit, yield results.

    Yields tuples (option_value, option_text, headers, rows).
    `reset_url` (optional) is reloaded between iterations to clear the
    form. If None, the form's go_back() is used. Failures on a single
    option are logged and skipped — the iterator does not raise."""
    # Snapshot the dropdown options once. We can't read them during
    # iteration because the DOM may be replaced post-submit.
    options = []
    for sel in page.query_selector_all("select"):
        if (sel.get_attribute("name") or "") != dropdown_name:
            continue
        for o in sel.query_selector_all("option"):
            v = o.get_attribute("value") or ""
            t = (o.inner_text() or "").strip()
            if v in skip_values:
                continue
            options.append((v, t))
        break

    for value, text in options:
        try:
            select_option(page, dropdown_name, value)
            submit_form(page, submit_selector,
                        wait_after_ms=between_iterations_ms)
            headers, rows = extract_table_after_submit(
                page, header_must_contain=header_must_contain)
            yield value, text, headers, rows
        except Exception as e:
            print(f"[aspnet] dropdown={dropdown_name} value={value!r} "
                  f"err: {type(e).__name__}: {str(e)[:140]}")
        # Reset for next iteration.
        try:
            if reset_url:
                page.goto(reset_url, wait_until="networkidle",
                          timeout=30_000)
            else:
                page.go_back(wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(between_iterations_ms)
        except Exception:
            # Last resort: hard reload.
            try:
                page.reload(wait_until="networkidle", timeout=30_000)
            except Exception:
                pass
