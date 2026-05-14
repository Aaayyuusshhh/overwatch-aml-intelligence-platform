"""
Quick per-source name cleaners for NSE, ICEX, and Meghalaya Police.

Each has a different name-pollution pattern that doesn't fit the SEBI
order-title cleaner. Each cleaner edits a list of target CSVs in
place, moving the polluted original into the details field.

RVNL is intentionally NOT handled here — its scraper looks broken at
the source (HTML disclaimer text and pipe-table fragments leaking
into the name field). Listed under "needs scraper fix" in the report.
"""

import argparse
import csv
import glob
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# ---------- NSE cleaner -----------------------------------------------------
# Pattern: "<NAME> ( Address: …)" or "<NAME>. Address: …" or
#          "<NAME>; Address: …" or "<NAME>.Listed at BSE;  Address: …".
# Action:  truncate at the address marker.
_NSE_ADDR_SPLIT = re.compile(
    r"\s*[\(;.,]\s*(?:Address|Director|Listed)\s*:?\s*",
    re.I,
)
_NSE_LEADING_TITLES = re.compile(r"^\s*(?:Mr\.?|Mrs\.?|Ms\.?|Shri\.?|Sri\.?|Smt\.?|M/S\.?)\s+", re.I)


def clean_nse_name(name):
    if not name or len(name) <= 80:
        return name, False
    m = _NSE_ADDR_SPLIT.search(name)
    if not m:
        return name, False
    head = name[: m.start()].strip().strip(",;.- ")
    head = _NSE_LEADING_TITLES.sub("", head).strip()
    if len(head) < 3:
        return name, False
    return head, True


# ---------- ICEX cleaner ----------------------------------------------------
# Pattern: "<Company Name>. <Address starting with digit/uppercase>"
#          or "<Company Name> <digit/comma-introduced address>"
# Action:  truncate at the first ". " (company-ending period) or at
#          ", <digit>" sequence that introduces a postal-style address.
_ICEX_END_MARKERS = [
    re.compile(r"\.\s+(?=\d)"),               # "Limited. 6B,…"
    re.compile(r"\s+(?=\d{1,3}\s*[,/])"),     # "Jindal Commodities 34 ,…" / "33/1,…"
    re.compile(r"\s+(?=\d{2,}\b)"),           # "Limited 33 C R Avenue,…"
    re.compile(r",\s+(?=\d)"),                # ", 33 something"
]


def clean_icex_name(name):
    if not name or len(name) <= 80:
        return name, False
    best_cut = None
    for pat in _ICEX_END_MARKERS:
        m = pat.search(name)
        if m and (best_cut is None or m.start() < best_cut):
            best_cut = m.start()
    if best_cut is None or best_cut < 10:
        return name, False
    return name[:best_cut].strip().strip(".,;:- "), True


# ---------- Meghalaya Police cleaner ----------------------------------------
# Pattern: "Look out notice (for|of) wanted person namely <NAME> in
#          connection with <PS>" or "<NAME> (NN Yrs) S/O <Father> of
#          <Village>. PS <PS>".
_MEG_NAMELY = re.compile(
    r"\bnamely\s+(?P<n>.+?)\s+"
    r"(?:in\s+connection\s+with|involved\s+in|@|$)", re.I,
)
_MEG_PARENS_AGE = re.compile(
    r"^\s*(?P<n>.+?)\s*\(\s*\d{1,3}\s*Yrs?\b", re.I,
)
_MEG_LEADING_TITLES = re.compile(r"^\s*(?:Shri\.?|Sri\.?|Smt\.?|Ms\.?|Mr\.?)\s+", re.I)


def clean_meg_name(name):
    if not name or len(name) <= 80:
        return name, False
    m = _MEG_NAMELY.search(name)
    if m:
        cand = m.group("n").strip()
        cand = _MEG_LEADING_TITLES.sub("", cand).strip()
        if len(cand) >= 3:
            return cand, True
    m = _MEG_PARENS_AGE.search(name)
    if m:
        cand = m.group("n").strip()
        cand = _MEG_LEADING_TITLES.sub("", cand).strip()
        if len(cand) >= 3:
            return cand, True
    return name, False


# ---------- CSV walker ------------------------------------------------------
ORIGINAL_PREFIX = "Original name: "


def _apply(path, cleaner, agency_filter=None, dry_run=False):
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    cleaned = 0
    before_max = 0
    after_max = 0
    for r in rows:
        nm = r.get("name", "") or ""
        before_max = max(before_max, len(nm))
        if agency_filter and r.get("source_agency") != agency_filter:
            after_max = max(after_max, len(nm))
            continue
        new, changed = cleaner(nm)
        if changed:
            cleaned += 1
            existing = r.get("details", "") or ""
            if ORIGINAL_PREFIX not in existing:
                r["details"] = (
                    f"{ORIGINAL_PREFIX}{nm}"
                    + (f" | {existing}" if existing else "")
                )
            r["name"] = new
            after_max = max(after_max, len(new))
        else:
            after_max = max(after_max, len(nm))
    if not dry_run and cleaned:
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    return {"path": path, "total": len(rows), "cleaned": cleaned,
            "max_len_before": before_max, "max_len_after": after_max}


TARGETS = [
    # (cleaner, agency_filter, csv_glob)
    (clean_nse_name,  "NSE",                                  "nse_*.csv"),
    (clean_icex_name, "Indian Commodity Exchange Ltd (ICEX)", "icex_*.csv"),
    (clean_meg_name,  "Meghalaya Police",                     "mp_list_of_wanted_person_217.csv"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("pick --apply or --dry-run")
    mode = "DRY-RUN" if args.dry_run else "APPLIED"

    for cleaner, agency, glob_pat in TARGETS:
        files = sorted(glob.glob(os.path.join(DATA_DIR, glob_pat)))
        if not files:
            print(f"  ({cleaner.__name__}) no files match {glob_pat}")
            continue
        for p in files:
            s = _apply(p, cleaner, agency_filter=agency,
                       dry_run=args.dry_run)
            print(f"  {os.path.basename(p):50}  total={s['total']:>5}  "
                  f"cleaned={s['cleaned']:>4}  max {s['max_len_before']} -> {s['max_len_after']}")
    print(f"--- mode={mode} ---")


if __name__ == "__main__":
    main()
