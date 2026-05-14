"""
classify.py - URL discovery + classification for all 244 India watchlist
sources.

Reads india_watchlist_sources_complete.csv and produces sources.json
with one entry per source containing id, agency, list_name, url, type,
status, change_detection, change_detection_selector, scraper,
expected_min_records, ppt_number, notes.

Per ARCHITECTURE.md S4.2 / PRD S6.1. Classification only - no scraping
of content.

URL discovery:
  1. Preserve the 6 existing custom-scraper sources verbatim from
     sources.json (CBI x3, MHA x2, NIA x1).
  2. Skipped sources from the input CSV get type='skipped'.
  3. Otherwise look up the agency's primary domain in AGENCY_DOMAINS.
     a. Try up to 3 constructed candidate URLs built from kebab-case
        slugs of the watchlist name.
     b. If none returns 200 OK, fetch the agency homepage once and
        search anchor text for watchlist keywords. First substring
        match wins.
     c. Still nothing -> mark url_not_found.
  4. Agency not in map -> url_not_found.

Classification of a found URL:
  - HTTP 4xx / 5xx                                -> dead
  - Content-Type contains 'pdf'                    -> pdf
  - Body has password input or captcha markers     -> restricted
  - Body has any <table>                           -> html
  - Body has < 200 chars text and looks SPA-shell  -> js
  - default                                        -> html

Politeness: 2 s sleep between every Scrapling fetch.
Per-source budget: 120 s wall clock for discovery + classification.
Resumable: existing classification cache loaded from sources.json so
reruns skip sources already typed.

Usage:
    python classify.py                # full run
    python classify.py --dry-run      # build candidate URLs, no fetch
    python classify.py --limit N      # process only first N sources
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(PROJECT_ROOT, "india_watchlist_sources_complete.csv")
OUTPUT_JSON = os.path.join(PROJECT_ROOT, "sources.json")

POLITENESS_SECONDS = 2.0
PER_SOURCE_BUDGET = 120.0
MAX_CANDIDATES = 3            # constructed-URL probes per source
FETCH_TIMEOUT = 20            # passed to Scrapling Fetcher.get
HOMEPAGE_LINK_LIMIT = 200     # how many <a> on homepage to scan

# ---------------------------------------------------------------------------
# AGENCY DOMAIN MAP
# ---------------------------------------------------------------------------
# Keys are the verbatim 'agency' strings from
# india_watchlist_sources_complete.csv normalised by _norm_agency():
# lowercased, collapsed whitespace. Values are the primary domain
# (without scheme) or None when no public site is known.
AGENCY_DOMAINS = {
    "all india bank employees association (aibea)": "aibea.in",
    "all india council for technical education (aicte)": "aicte-india.org",
    "bureau of indian standards (bis)": "bis.gov.in",
    "central board of direct taxes (cbdt)": "incometaxindia.gov.in",
    "central board of excise and customs (cbec)": "cbic.gov.in",
    "central bureau of investigation (cbi)": "cbi.gov.in",
    "central vigilance commission (cvc)": "cvc.gov.in",
    "competition commission of india (cci)": "cci.gov.in",
    "credit information bureau india limited (cibil)": "cibil.com",
    "debts recovery tribunals (drts)": "drt.gov.in",
    "directorate general of foreign trade (dgft)": "dgft.gov.in",
    "directorate of enforcement (ed)": "enforcementdirectorate.gov.in",
    "economic offence wing": "eow.tn.gov.in",
    "employees provident fund organisation (epfo)": "epfindia.gov.in",
    "employees' state insurance corporation": "esic.gov.in",
    "financial intelligence unit (fiu)": "fiuindia.gov.in",
    "government e marketplace": "gem.gov.in",
    "government of maharashtra": "mahagst.gov.in",
    "income tax department": "incometaxindia.gov.in",
    "insurance regulatory and development authority of india (irdai)": "irdai.gov.in",
    "ministry of corporate affairs (mca)": "mca.gov.in",
    "ministry of defence (mod)": "mod.gov.in",
    "ministry of external affairs (mea)": "mea.gov.in",
    "ministry of finance (mof)": "dfs.gov.in",
    "ministry of home affairs (mha)": "mha.gov.in",
    "ministry of social justice & empowerment (msj&e)": "socialjustice.gov.in",
    "ministry of women and child development government of india (mwcd)": "wcd.gov.in",
    "narcotics control bureau (ncb)": "narcoticsindia.nic.in",
    "national bank for agriculture and rural development": "nabard.org",
    "national bank for agriculture and rural development (nabard)": "nabard.org",
    "national company law tribunal (nclt)": "nclt.gov.in",
    "national crime records bureau (ncrb)": "ncrb.gov.in",
    "national financial reporting authority (nfra)": "nfra.gov.in",
    "national housing bank (nhb)": "nhb.org.in",
    "national institute of electronics and information technology (nielit)": "nielit.gov.in",
    "national institution for transforming india (niti) aayog": "niti.gov.in",
    "national investigation agency (nia)": "nia.gov.in",
    "reserve bank of india (rbi)": "rbi.org.in",
    "securities and exchange board of india (sebi)": "sebi.gov.in",
    "serious fraud investigation office (sfio)": "sfio.gov.in",
    "suspected shell companies check (ssc)": None,     # umbrella, no portal
    "university grants commission (ugc)": "ugc.gov.in",
    "wildlife crime control bureau (wccb)": "wccb.gov.in",
    "bank of baroda (bob)": "bankofbaroda.in",
    "bank of india (boi)": "bankofindia.co.in",
    "bank of maharashtra (bom)": "bankofmaharashtra.in",
    "indian bank (ib)": "indianbank.in",
    "indian overseas bank (iob)": "iob.in",
    "punjab & sindh bank (psb)": "punjabandsindbank.com",
    "uco bank (uco)": "ucobank.com",
    "airport authority of india (aai)": "aai.aero",
    "bharat heavy electricals limited (bhel)": "bhel.com",
    "coal india limited (cil)": "coalindia.in",
    "delhi metro rail corporation (dmrc)": "delhimetrorail.com",
    "gas authority of india limited (gail)": "gailonline.com",
    "hindustan copper ltd (hcl)": "hindustancopper.com",
    "indian oil corporation limited (iocl)": "iocl.com",
    "national highways & infrastructure development corporation limited (nhidcl)": "nhidcl.com",
    "national thermal power corporation limited (ntpc)": "ntpc.co.in",
    "nlc india limited (nlc)": "nlcindia.in",
    "oil and natural gas corporation (ongc)": "ongcindia.com",
    "power grid corporation of india ltd (powergrid)": "powergrid.in",
    "rail vikas nigam limited (rvnl)": "rvnl.org",
    "rashtriya ispat nigam limited (rinl)": "vizagsteel.com",
    "rural electrification corporation (rec)": "recindia.nic.in",
    "telecom regulatory authority of india (trai)": "trai.gov.in",
    "uttar pradesh power corporation limited (uppcl)": "uppcl.org",
    "bombay stock exchange (bse)": "bseindia.com",
    "central depository services (india) limited (cdsl)": "cdslindia.com",
    "dse estates limited (del)": "dseindia.com",
    "indian clearing corporation limited (iccl)": "icclindia.com",
    "indian commodity exchange ltd (icex)": "icexindia.com",
    "metropolitan stock exchange (mse)": "msei.in",
    "multi commodity exchange (mcx)": "mcxindia.com",
    "national commodity and derivatives exchange (ncdex)": "ncdex.com",
    "national commodity clearing limited (nccl)": "nccl.co.in",
    "national securities depository limited (nsdl)": "nsdl.co.in",
    "national spot exchange limited (nsel)": "nationalspotexchange.com",
    "national stock exchange (nse)": "nseindia.com",
    "nse clearing limited (nsecl)": "nscclindia.com",
    "andaman and nicobar police (anp)": "police.and.nic.in",
    "arunachal pradesh police (app)": "arunpol.nic.in",
    "assam police (ap)": "assampolice.gov.in",
    "chandigarh police (cp)": "chandigarhpolice.gov.in",
    "delhi police (dp)": "delhipolice.gov.in",
    "gurugram police (gp)": "gurugrampolice.gov.in",
    "haryana police": "haryanapolice.gov.in",
    "karnataka police (kp)": "ksp.gov.in",
    "kerala police (kp)": "keralapolice.gov.in",
    "kolkata police (kp)": "kolkatapolice.gov",
    "meghalaya police (mp)": "megpolice.gov.in",
    "mumbai police (mp)": "mumbaipolice.gov.in",
    "nagaland police (np)": "nagapol.gov.in",
    "tripura police (tp)": "tripurapolice.gov.in",
    "uttar pradesh police (up police)": "uppolice.gov.in",
    "helpful foundation (hf)": None,
    "press information bureau": "pib.gov.in",
    "criminal investigation department maharashtra state (cidms)": "mahacidcrime.gov.in",
    "jharkhand police (jp)": "jhpolice.gov.in",
    "rajasthan police (rp)": "police.rajasthan.gov.in",
    "criminal investigation department of west bengal (cidwb)": "cidwestbengal.gov.in",
    "association of mutual funds in india (amfi)": "amfiindia.com",
    "insolvency and bankruptcy board of india (ibbi)": "ibbi.gov.in",
}

# CSV ppt_number -> existing source_id (for the 6 already-scraped sources).
EXISTING_BY_PPT = {
    12: "cbi_announced_rewards",
    13: "cbi_red_notices",
    15: "cbi_yellow_notices",
    68: "mha_uapa_individual_terrorists",
    69: "mha_banned_orgs",
    101: "nia_most_wanted",
    # #70 'Individual Terrorists Under UAPA' is a duplicate of #68
    # but does not share an output CSV - we'll mark it 'duplicate'
    # in notes and reuse #68's URL/type so the daily run doesn't
    # double-scrape.
}

DUPLICATE_OF = {
    70: 68,   # MHA Individual Terrorists Under UAPA == #68
}

# CSV rows whose status is 'skipped' get carried over (no URL probe).
SKIPPED_PPT = {11, 14}

# ---------------------------------------------------------------------------
# Strings used to identify login / captcha / SPA shells.
# ---------------------------------------------------------------------------
LOGIN_MARKERS_HTML = (
    'type="password"', "type='password'",
    'name="password"', "name='password'",
    'name="captcha"', "name='captcha'",
    "g-recaptcha",
    "h-captcha",
    'id="login"', "id='login'",
)
LOGIN_MARKERS_TEXT = (
    "please log in", "please login", "sign in to continue",
    "session expired", "user id", "captcha",
)
SPA_MARKERS = (
    'id="root"', "id='root'",
    'id="app"', "id='app'",
    'data-reactroot',
    'ng-app=', 'ng-version=',
    'data-server-rendered',
)


def _norm_agency(s):
    return re.sub(r"\s+", " ", s.strip().lower())


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------
def slugify(text, max_words=None):
    """Lowercase, drop punctuation, kebab-case. max_words trims to first
    N words for shorter URL fragments."""
    text = re.sub(r"[^A-Za-z0-9\s\-]", " ", text)
    words = text.lower().split()
    if max_words:
        words = words[:max_words]
    return "-".join(words)


def candidate_paths(list_name):
    """Return up to MAX_CANDIDATES candidate URL paths."""
    full = slugify(list_name)
    short = slugify(list_name, max_words=3)
    paths = []
    seen = set()
    for s in (full, short):
        if s and s not in seen:
            paths.append(f"/{s}")
            seen.add(s)
    return paths[:MAX_CANDIDATES]


def candidate_urls(domain, list_name):
    """Combine domain x candidate path x scheme/host variants."""
    if not domain:
        return []
    paths = candidate_paths(list_name)
    urls = []
    seen = set()
    for path in paths:
        for host in (domain, "www." + domain if not domain.startswith("www.") else domain):
            for prefix in ("", "/en"):
                u = f"https://{host}{prefix}{path}"
                if u not in seen:
                    urls.append(u)
                    seen.add(u)
    return urls[: MAX_CANDIDATES * 2]   # cap total candidates


# ---------------------------------------------------------------------------
# Fetch wrapper with timeout + politeness
# ---------------------------------------------------------------------------
_last_fetch_at = 0.0


def _polite_sleep():
    global _last_fetch_at
    elapsed = time.time() - _last_fetch_at
    if elapsed < POLITENESS_SECONDS:
        time.sleep(POLITENESS_SECONDS - elapsed)


_dns_failed_hosts = set()   # hosts we've already proven unresolvable


def fetch(url):
    """Fetch with timeout, returning Scrapling Response or None on error.

    Uses retries=0 to fail fast on DNS / refused-connection errors so
    classification of 244 sources doesn't get bogged down by the 3x
    auto-retry that Scrapling does by default. We also short-circuit
    further fetches to a host once it has DNS-failed."""
    global _last_fetch_at
    host = urlparse(url).hostname or ""
    if host in _dns_failed_hosts:
        print(f"      skipped (host DNS-failed earlier): {host}")
        return None
    _polite_sleep()
    try:
        # verify=False bypasses TLS verification - acceptable for
        # classification probes where we are not exchanging credentials.
        # Many Indian government sites serve expired or self-signed certs.
        resp = Fetcher.get(url, timeout=FETCH_TIMEOUT, retries=1,
                           retry_delay=0, verify=False)
    except Exception as e:
        _last_fetch_at = time.time()
        msg = str(e)
        print(f"      fetch_err {type(e).__name__}: {msg[:120]}")
        if "Could not resolve host" in msg or "Name or service not known" in msg:
            _dns_failed_hosts.add(host)
        return None
    _last_fetch_at = time.time()
    return resp


def _status(resp):
    return getattr(resp, "status", None) or getattr(resp, "status_code", None)


def _content_type(resp):
    headers = getattr(resp, "headers", {}) or {}
    return (headers.get("content-type") or headers.get("Content-Type") or "").lower()


def _body_str(resp):
    body = getattr(resp, "html_content", None) or getattr(resp, "body", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return body or ""


# ---------------------------------------------------------------------------
# Classification of a response
# ---------------------------------------------------------------------------
def classify_response(resp):
    """Return one of: dead, pdf, restricted, html, js."""
    status = _status(resp)
    if status is None or status >= 400:
        return "dead"

    ctype = _content_type(resp)
    if "pdf" in ctype or ctype.startswith("application/pdf"):
        return "pdf"

    body_lc = _body_str(resp).lower()

    # Restricted: login form or captcha markers in raw HTML
    for marker in LOGIN_MARKERS_HTML:
        if marker.lower() in body_lc:
            return "restricted"
    visible = re.sub(r"<[^>]+>", " ", body_lc)
    visible = re.sub(r"\s+", " ", visible).strip()
    for marker in LOGIN_MARKERS_TEXT:
        if marker in visible:
            # Login phrase only counts if there isn't a clear data table
            # already on the page (some sites display "user id" in nav).
            if "<table" not in body_lc:
                return "restricted"

    if "<table" in body_lc:
        return "html"

    # Empty / SPA shell -> js
    if len(visible) < 300:
        for spa in SPA_MARKERS:
            if spa.lower() in body_lc:
                return "js"
        if len(visible) < 80:
            return "js"

    return "html"


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------
def discover_via_homepage(domain, list_name, deadline):
    """Fetch agency homepage, scan <a> for keyword overlap with list_name.
    Returns first matching absolute URL or None."""
    if not domain:
        return None
    keywords = [w for w in re.findall(r"[a-zA-Z]+", list_name.lower())
                if len(w) > 3]
    if not keywords:
        return None

    homepage = f"https://{domain}/"
    resp = fetch(homepage)
    if resp is None or _status(resp) is None or _status(resp) >= 400:
        # try without TLS
        homepage = f"http://{domain}/"
        if time.time() > deadline:
            return None
        resp = fetch(homepage)
        if resp is None or _status(resp) is None or _status(resp) >= 400:
            return None

    try:
        anchors = resp.find_all("a")
    except Exception:
        return None
    seen = set()
    best = None
    best_score = 0
    for a in anchors[:HOMEPAGE_LINK_LIMIT]:
        try:
            text = (a.text or "").lower()
            href = a.attrib.get("href", "") if hasattr(a, "attrib") else ""
        except Exception:
            continue
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        score = sum(1 for k in keywords if k in text)
        if score > best_score:
            absolute = urljoin(homepage, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            best = absolute
            best_score = score
    if best_score >= 2:    # at least two keywords overlap
        return best
    return None


def discover_url(agency, list_name, deadline):
    """Try candidate construction first, homepage scan second.
    Returns (url, method, resp) or (None, None, None). resp is the
    successful Scrapling response so the caller can classify without
    a duplicate fetch."""
    domain = AGENCY_DOMAINS.get(_norm_agency(agency))
    if not domain:
        return None, None, None

    # Candidate construction
    for url in candidate_urls(domain, list_name):
        if time.time() > deadline:
            return None, None, None
        resp = fetch(url)
        if resp is None:
            continue
        status = _status(resp)
        if status and 200 <= status < 400:
            return url, "constructed", resp

    if time.time() > deadline:
        return None, None, None

    # Homepage anchor scan
    homepage_url = discover_via_homepage(domain, list_name, deadline)
    if homepage_url:
        return homepage_url, "homepage_scan", None
    return None, None, None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def make_id(agency, list_name, ppt_number):
    """Stable id from agency acronym + watchlist slug + ppt number."""
    if ppt_number in EXISTING_BY_PPT:
        return EXISTING_BY_PPT[ppt_number]
    m = re.search(r"\(([A-Za-z0-9& ]+)\)\s*$", agency)
    if m:
        acronym = re.sub(r"[^a-z0-9]+", "", m.group(1).lower())
    else:
        acronym = re.sub(r"[^a-z0-9]+", "_",
                         agency.lower().split()[0])[:8]
    slug = slugify(list_name, max_words=4).replace("-", "_")
    base = f"{acronym}_{slug}" if acronym else slug
    return f"{base}_{ppt_number}"   # ppt suffix guarantees uniqueness


def load_existing_sources():
    """Load current sources.json so we can preserve the 6 known entries
    plus reuse any cached classifications from prior runs."""
    if not os.path.exists(OUTPUT_JSON):
        return {}
    with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {s["id"]: s for s in data.get("sources", [])}


def load_input_csv():
    rows = []
    with open(INPUT_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["ppt_number"] = int(row["ppt_number"])
            rows.append(row)
    return rows


def process(rows, existing, limit=None, dry_run=False):
    out = []
    for i, row in enumerate(rows):
        if limit and i >= limit:
            break
        ppt = row["ppt_number"]
        agency = row["agency"]
        list_name = row["watchlist_details"]
        sid = make_id(agency, list_name, ppt)

        # Carry over the 6 existing custom-scraper entries verbatim.
        if sid in existing and ppt in EXISTING_BY_PPT:
            entry = dict(existing[sid])
            entry["ppt_number"] = ppt
            entry.setdefault("notes", row.get("notes", ""))
            print(f"[{ppt:3d}] {sid}: PRESERVED ({entry.get('type')})")
            out.append(entry)
            continue

        # Skipped sources from input CSV
        if ppt in SKIPPED_PPT or row.get("status") == "skipped":
            entry = {
                "id": sid,
                "ppt_number": ppt,
                "agency": agency,
                "list_name": list_name,
                "url": None,
                "type": "skipped",
                "scraper": None,
                "expected_min_records": None,
                "status": "skipped",
                "change_detection": False,
                "change_detection_selector": None,
                "notes": row.get("notes", ""),
            }
            print(f"[{ppt:3d}] {sid}: SKIPPED ({row.get('notes','')[:60]})")
            out.append(entry)
            continue

        # Duplicate-of-another-source entries (#70 -> #68)
        if ppt in DUPLICATE_OF:
            other_ppt = DUPLICATE_OF[ppt]
            other_sid = EXISTING_BY_PPT.get(other_ppt)
            other = existing.get(other_sid, {}) if other_sid else {}
            entry = {
                "id": sid,
                "ppt_number": ppt,
                "agency": agency,
                "list_name": list_name,
                "url": other.get("url"),
                "type": "duplicate",
                "scraper": None,
                "expected_min_records": None,
                "status": "skipped",
                "change_detection": False,
                "change_detection_selector": None,
                "notes": f"Duplicate of #{other_ppt} ({other_sid}); not scraped separately.",
            }
            print(f"[{ppt:3d}] {sid}: DUPLICATE of #{other_ppt}")
            out.append(entry)
            continue

        # If a previous classify run already typed this id, reuse it.
        cached = existing.get(sid)
        if cached and cached.get("type") not in (None, "unknown", "pending_recon"):
            print(f"[{ppt:3d}] {sid}: CACHED type={cached['type']} url={cached.get('url')}")
            cached["ppt_number"] = ppt
            out.append(cached)
            continue

        # Fresh discovery + classification
        deadline = time.time() + PER_SOURCE_BUDGET
        if dry_run:
            url, method, resp = None, None, None
        else:
            url, method, resp = discover_url(agency, list_name, deadline)

        if dry_run:
            domain = AGENCY_DOMAINS.get(_norm_agency(agency))
            cands = candidate_urls(domain, list_name) if domain else []
            print(f"[{ppt:3d}] {sid}: dry-run domain={domain} candidates={cands[:3]}")
            entry = {
                "id": sid,
                "ppt_number": ppt,
                "agency": agency,
                "list_name": list_name,
                "url": None,
                "type": "pending_recon",
                "scraper": None,
                "expected_min_records": None,
                "status": "pending_recon",
                "change_detection": False,
                "change_detection_selector": None,
                "notes": "dry-run",
            }
            out.append(entry)
            continue

        if not url:
            no_domain = AGENCY_DOMAINS.get(_norm_agency(agency)) is None
            entry = {
                "id": sid,
                "ppt_number": ppt,
                "agency": agency,
                "list_name": list_name,
                "url": None,
                "type": "url_not_found",
                "scraper": None,
                "expected_min_records": None,
                "status": "url_not_found",
                "change_detection": False,
                "change_detection_selector": None,
                "notes": "agency domain not in map" if no_domain
                        else "discovery exhausted (constructed + homepage scan)",
            }
            print(f"[{ppt:3d}] {sid}: URL_NOT_FOUND ({entry['notes']})")
            out.append(entry)
            continue

        # If discovery handed back a successful response, classify that
        # directly. Otherwise (homepage_scan path) fetch the discovered
        # link once for classification.
        if resp is None:
            resp = fetch(url)
        if resp is None:
            entry_type = "dead"
        else:
            entry_type = classify_response(resp)

        status_field = "active" if entry_type in ("html", "pdf") else entry_type
        entry = {
            "id": sid,
            "ppt_number": ppt,
            "agency": agency,
            "list_name": list_name,
            "url": url,
            "type": entry_type,
            "scraper": None,
            "expected_min_records": None,
            "status": status_field,
            "change_detection": entry_type in ("html",),
            "change_detection_selector": None,
            "notes": f"discovered via {method}",
        }
        print(f"[{ppt:3d}] {sid}: {entry_type.upper():>10}  url={url}")
        out.append(entry)

    return out


def write_sources(entries):
    payload = {"sources": entries}
    tmp = OUTPUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, OUTPUT_JSON)
    print(f"\nWrote {len(entries)} entries to {OUTPUT_JSON}")


def summarise(entries):
    counts = {}
    for e in entries:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    print("\n" + "=" * 60)
    print("CLASSIFICATION SUMMARY")
    print("=" * 60)
    for k in sorted(counts):
        print(f"  {k:18} {counts[k]:4d}")
    print(f"  {'TOTAL':18} {sum(counts.values()):4d}")
    custom = sum(1 for e in entries if e.get("scraper"))
    print(f"  with custom scraper: {custom}")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print candidates only, no HTTP fetches")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process first N input rows")
    args = ap.parse_args()

    print(f"classify.py start  {datetime.now().isoformat(timespec='seconds')}")
    rows = load_input_csv()
    print(f"input rows: {len(rows)}")
    existing = load_existing_sources()
    print(f"existing sources.json entries: {len(existing)}")
    print("-" * 60)

    entries = process(rows, existing, limit=args.limit, dry_run=args.dry_run)
    # Only write sources.json on a full live run (no --limit, no --dry-run)
    # so partial smoke tests can't clobber the canonical config.
    if args.limit or args.dry_run:
        print(f"\n[smoke run: limit={args.limit} dry_run={args.dry_run}] "
              f"sources.json NOT written")
    else:
        write_sources(entries)
    summarise(entries)


if __name__ == "__main__":
    main()
