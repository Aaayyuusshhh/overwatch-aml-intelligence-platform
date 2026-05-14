"""
SEBI order-title -> entity-name cleaner.

The SEBI scrapers historically stored the full order title in the
'name' field, e.g. "Adjudication Order in respect of <NAME> in the
matter of <COMPANY>". For screening hits to be useful the name needs
to contain the entity (person / company) the order targets, not the
title prose.

Public entry points:

    extract_entity_name(title) -> (name, pattern_label)
        Returns (cleaned_name, label_of_pattern_used). If no pattern
        matched, returns (title, "no_match").

    clean_csv(path, dry_run=False) -> dict
        Rewrites a SEBI CSV in place, moving the full title into
        details when the name was long/title-shaped. Returns a stats
        dict.

Run from CLI for a per-file walk over data/sebi_*.csv:
    python scripts/sebi_name_cleaner.py --dry-run
    python scripts/sebi_name_cleaner.py --apply
    python scripts/sebi_name_cleaner.py --apply --file data/sebi_orders_of_ao_119.csv
"""

import argparse
import csv
import glob
import os
import re
import sys

# ----- priority-ordered extraction patterns ---------------------------------
# Each pattern matches a substring of the title and captures the entity
# segment in group 'n'. Order matters — earlier patterns are tried first
# so they win over the less specific ones below. Groups are written to
# capture as little as possible so trailing prose ("in the matter of …",
# "(PAN: …)") stays out of the captured name.

# We anchor patterns with explicit lower-cased look-ahead, but the regex
# itself is case-insensitive.

PATTERNS = [
    # 1. "<...> in respect of X in the matter of Y" — X is the entity.
    ("in_respect_of+matter_of",
     re.compile(r"\bin\s+(?:the\s+)?re(?:spect|psect)\s+of\s+(?P<n>.+?)\s+"
                r"in\s+(?:the\s+)?matter\s+of\b", re.I)),
    # 2. "with respect to X in the matter of Y" — X is the entity.
    ("with_respect_to+matter_of",
     re.compile(r"\bwith\s+respect\s+to\s+(?P<n>.+?)\s+"
                r"in\s+(?:the\s+)?matter\s+of\b", re.I)),
    # 3. "against X in the matter of Y" — X is the entity. The X part
    #    may include "(India)" or "(HUF)" style parens (legitimately part
    #    of company names) but must stop at "(PAN:" or "in the matter of".
    ("against+matter_of",
     re.compile(r"\bagainst\s+(?P<n>.+?)\s+"
                r"(?:\(\s*PAN\b|,\s*PAN\s*[:.]|in\s+(?:the\s+)?matter\s+of\b)",
                re.I)),
    # 4. "drawn against X (PAN:…) in the matter of Y" — covered by
    #    pattern 3 because we stop at '('. The PAN block leaks into
    #    details; we strip it in post-processing.
    # 5. "by X In The Matter of Y" — used by Settlement / Consent.
    ("by+matter_of",
     re.compile(r"\b(?:submitted\s+by|by)\s+(?P<n>.+?)\s+"
                r"in\s+(?:the\s+)?matter\s+of\b", re.I)),
    # 6. "issued <NAME> [Defaulter]" — Completion of Recovery Certificate.
    ("issued+defaulter",
     re.compile(r"\bissued\s+(?:to\s+)?(?P<n>.+?)\s*\[\s*Defaulters?\s*\]",
                re.I)),
    # 7. "<...> – RC No. ... - X" or "RC No. X of YYYY- X" — Recovery proc.
    ("rc_no+dash+name",
     re.compile(r"RC\s*No\.?\s*\d+\s*(?:of\s*\d{4})?\s*[-–]\s*"
                r"(?P<n>[^-–]+?)\s*$", re.I)),
    # 8. "Certificate No. X of Y - <NAME> (PAN:" — Recovery; '_' is a
    #    common OCR/scrape separator on SEBI pages.
    ("certificate_no+against",
     re.compile(r"Certificate\s+No\.?\s*\S+\s*of\s*\d{4}\s*"
                r"(?:against|_|-|–)\s*"
                r"(?:Notice\s+of\s+Demand\s+against\s+)?"
                r"(?P<n>[^()_]+?)\s*(?:\(|_|in\s+(?:the\s+)?matter\s+of\b)",
                re.I)),
    # 9. "in respect of X" (no matter-of suffix)
    ("in_respect_of",
     re.compile(r"\bin\s+(?:the\s+)?re(?:spect|psect)\s+of\s+(?P<n>.+?)\s*$",
                re.I)),
    # 10. "with respect to X"
    ("with_respect_to",
     re.compile(r"\bwith\s+respect\s+to\s+(?P<n>.+?)\s*$", re.I)),
    # 11. "against X" (end of string)
    ("against",
     re.compile(r"\bagainst\s+(?P<n>[^()]+?)\s*(?:\(PAN[^)]*\))?\s*$",
                re.I)),
    # 12. "issued to X in the matter of Y" — stop at matter-of boundary.
    ("issued_to+matter_of",
     re.compile(r"\bissued\s+(?:to\s+)?(?P<n>.+?)\s+"
                r"in\s+(?:the\s+)?matter\s+of\b", re.I)),
    # 13. "issued to X" (end of string)
    ("issued_to",
     re.compile(r"\bissued\s+to\s+(?P<n>.+?)\s*"
                r"(?:\(\s*PAN\b|,\s*PAN\b|$)", re.I)),
    # 14. "to X" at end (Unserved Hearing Notice to ...)
    ("notice_to",
     re.compile(r"\b(?:Notice|letters?|letter)\s+(?:[A-Za-z]+\s+)?to\s+"
                r"(?P<n>.+?)\s*(?:in\s+(?:the\s+)?matter\s+of\b|$)", re.I)),
    # 15. "in the matter of X" — fallback when no entity prefix. X is
    #     typically the company. We accept "Ltd.", "Pvt." etc. by not
    #     stopping at periods — instead stop at "dated"/"under"/"vide"/
    #     SAT-appeal bracket, or end-of-string.
    ("in_the_matter_of",
     re.compile(r"\bin\s+(?:the\s+)?matter\s+of\s+(?P<n>.+?)\s*"
                r"(?:\bdated\b|\bunder\b|\bvide\b|\bpursuant\s+to\b|"
                r"\[SAT\b|$)", re.I)),
    # 15. "relating to X" — auction notices
    ("relating_to",
     re.compile(r"\brelating\s+to\s+(?P<n>.+?)\s*$", re.I)),
    # 16. "Accounts/Folio/Properties of X" — Recovery proceeding titles
    #     of the form "Notice of Attachment of Bank Accounts of <NAME>"
    #     and "Order Releasing … Accounts of <NAME>".
    ("accounts_of",
     re.compile(r"\b(?:Bank\s+(?:and\s+Demat\s+)?Accounts|Demat\s+Accounts?|"
                r"Mutual\s+Fund\s+Folio\(?s?\)?|Accounts?|Folio\(?s?\)?)\s+"
                r"of\s+(?P<n>.+?)\s*"
                r"(?:in\s+(?:the\s+)?matter\s+of\b|"
                r"in\s+Attachment\s+Proceedings\b|"
                r"\(\s*PAN\b|\bunder\b|$)", re.I)),
    # 17. "Property ... of X" — auction "for sale of … of <Entity>".
    ("auction_of_entity",
     re.compile(r"\b(?:Properties|Property|Movable|Immovable)\s+"
                r"(?:[A-Z][a-zA-Z ]*\s+)?of\s+(?P<n>.+?)\s*"
                r"(?:in\s+(?:the\s+)?matter\s+of\b|$)", re.I)),
    # 18. "for E-Auction ... of X" at end
    ("eauction_of",
     re.compile(r"E[-\s]?Auction\b[^.]*?\s+of\s+(?P<n>.+?)\s*$", re.I)),
    # 19. "SEBI vs/v. X & Ors." — Special-court judgments. The entity
    #     name is the defendant after "SEBI vs". Stops at "& Ors", "&
    #     Anr", or end of string.
    ("sebi_vs",
     re.compile(r"\bSEBI\s+(?:vs|v\.|v/s)\.?\s+(?P<n>.+?)\s*"
                r"(?:&\s*(?:Ors\.?|Anr\.?|Others)\.?|\s*$)", re.I)),
]

# Words that frequently appear after "issued to"/"against" but are NOT
# entities — reject these captures so the matcher falls through to the
# next pattern.
REJECT_LEADING = {
    "the", "a", "an", "his", "her", "its", "their", "this", "that", "these",
    "those", "purchaser", "purchasers", "complainant", "complainants",
    "applicant", "applicants", "respondent", "respondents", "appellant",
    "appellants", "petitioner", "managing", "director", "directors",
    "promoter", "promoters", "defaulter", "defaulters", "noticee",
    "noticees", "company", "rc",
}


# ---------- post-extraction cleanup ----------------------------------------

# Titular prefixes commonly attached to Indian names — strip them so the
# name reads as a name, but only at the start of the string (don't gut
# the middle of multi-name strings like "Mr X and Ms Y").
_LEADING_TITLES = re.compile(
    r"^\s*(?:Shri\.?|Sri\.?|Smt\.?|Ms\.?|Mr\.?|Mrs\.?|Dr\.?|CA\.?|"
    r"M/s\.?|M/s)\s+",
    re.I,
)

_PAN_BRACKET = re.compile(r"\(\s*PAN[^)]*\)|\[\s*PAN[^)\]]*\]", re.I)
_DEFAULTER_BRACKET = re.compile(r"\[\s*Defaulters?\s*\]", re.I)
_TRAILING_PERIOD = re.compile(r"[.\s]+$")
_TRAILING_DATE = re.compile(
    r"\b(?:dated|on)\s+\d{1,2}\.\d{1,2}\.\d{2,4}\s*$|"
    r"\bfor\s+FY\s*\d{4}[-–]\d{2,4}\s*$", re.I,
)

# Descriptive trailing prose to truncate: "<NAME>, erstwhile member of …"
# is just the entity name plus its prior role — the screening hit only
# needs the name. Splitting at the comma+keyword keeps "X" without also
# eating multi-entity comma lists ("X, Y and Z" stays intact because the
# keyword anchor 'erstwhile/past/former/viz.' isn't a name fragment).
_TRAILING_DESC = re.compile(
    r"\s*[,;]\s+(?:erstwhile|past|former|viz\.?|alias|now\s+known\s+as|"
    r"earlier\s+known\s+as|hereinafter|the\s+then|member\s+of\b|"
    r"director\s+of\b|proprietor\s+of\b).*$",
    re.I,
)

# Title-prose words that mean "this is a title, not a name". Used both
# to decide if we *need* to extract, and to reject extractions that
# accidentally captured prose.
TITLE_TRIGGER_WORDS = (
    "adjudication", "order", "notice", "certificate", "sebi ",
    "recovery", "interim", "confirmatory", "show cause",
    "auction", "settlement", "consent", "remittance", "release",
    "completion", "hearing", "unserved", "ex-parte", "public notice",
    "sale", "attachment", "summons",
)
# "Aggregate" markers — the title hides multiple unnamed entities behind
# a count. We fall back to "in the matter of" / company name in that case.
AGGREGATE_RE = re.compile(
    r"\b\d+\s+entit(?:y|ies)\b|\b(?:certain|various)\s+entit", re.I,
)


def _looks_like_title(name):
    """Return True if `name` reads like a title prose (which means it
    needs cleaning), False if it reads like a bare entity name."""
    if not name:
        return False
    if len(name) > 80:
        return True
    nl = name.lower().lstrip()
    return any(nl.startswith(w) for w in TITLE_TRIGGER_WORDS)


def _strip_titular(s):
    """Repeatedly strip 'Shri'/'Mr.'/'M/s.' prefixes at the start."""
    prev = None
    while s and s != prev:
        prev = s
        s = _LEADING_TITLES.sub("", s).strip()
    return s


def _post_clean(name):
    """Apply cleanup rules to an extracted name.

    Only invoked after a pattern matched. Returns "" when the extracted
    string is too short / clearly garbage."""
    if not name:
        return ""
    s = name.strip()
    # Drop "(PAN: …)" / "[PAN: …]" leakage anywhere.
    s = _PAN_BRACKET.sub("", s)
    s = _DEFAULTER_BRACKET.sub("", s)
    # Drop trailing date / FY phrases.
    s = _TRAILING_DATE.sub("", s)
    # Drop trailing descriptive prose ("…, erstwhile member of …").
    s = _TRAILING_DESC.sub("", s)
    # Drop a leading "Notice of Demand against " that occasionally leaks
    # through certain Recovery patterns.
    s = re.sub(r"^\s*Notice\s+of\s+Demand\s+against\s+", "", s, flags=re.I)
    # Strip honorifics / company markers at the *start* of the string
    # (a leading 'Shri' is noise, but 'Shri' inside 'A and Shri B' is
    # part of the second name and should stay).
    s = _strip_titular(s)
    # Collapse whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    # Strip trailing punctuation.
    s = _TRAILING_PERIOD.sub("", s)
    # Strip surrounding quotes / brackets.
    s = s.strip('"‘’“”[](){}')
    s = s.strip()
    # Close unbalanced trailing parens/brackets: if more openers than
    # closers, drop everything from the last unmatched opener. Names
    # like "Innoventive Venture Limited (Formerly Known as …" become
    # "Innoventive Venture Limited".
    for opener, closer in (("(", ")"), ("[", "]")):
        opens = s.count(opener)
        closes = s.count(closer)
        while opens > closes:
            idx = s.rfind(opener)
            if idx == -1:
                break
            s = s[:idx].rstrip(" -,–.")
            opens -= 1
    s = _TRAILING_PERIOD.sub("", s)
    s = s.strip()
    if len(s) < 3:
        return ""
    return s


# ---------- main extractor --------------------------------------------------

def extract_entity_name(title):
    """Return (extracted_name, pattern_label). Falls back to the
    original title with label 'no_match' when no pattern matches."""
    if not title:
        return title or "", "empty"
    raw = re.sub(r"\s+", " ", str(title)).strip()
    if not raw:
        return raw, "empty"

    # Fast path: short and not title-prose → already clean.
    if not _looks_like_title(raw):
        return raw, "already_clean"

    # Aggregate "N entities" cases: prefer "in the matter of …" fallback,
    # because the title doesn't name the entities at all.
    if AGGREGATE_RE.search(raw):
        m = PATTERNS[13][1].search(raw)  # in_the_matter_of (index 13)
        if m:
            cleaned = _post_clean(m.group("n"))
            if cleaned:
                return cleaned, "aggregate->in_the_matter_of"

    for label, pat in PATTERNS:
        m = pat.search(raw)
        if not m:
            continue
        cand = m.group("n")
        # Heuristic guard: skip captures that are themselves title-prose
        # (e.g. "in respect of [unserved interim order]…" — happens when
        # 'in respect of' appears twice).
        cand_low = cand.lower().lstrip()
        if any(cand_low.startswith(w) for w in TITLE_TRIGGER_WORDS):
            continue
        # Reject captures starting with generic non-entity words (e.g.
        # "purchaser of property", "the company", "respondent no. 4").
        first_word = re.split(r"[\s.,]+", cand_low, maxsplit=1)[0]
        if first_word in REJECT_LEADING:
            continue
        cleaned = _post_clean(cand)
        if not cleaned:
            continue
        # Reject pure punctuation / digit captures.
        if not re.search(r"[A-Za-z]{2,}", cleaned):
            continue
        # Reject obviously-not-a-name captures that are too long.
        if len(cleaned) > 180:
            continue
        return cleaned, label

    return raw, "no_match"


# ---------- CSV walker ------------------------------------------------------

NAME_FIELD = "name"
DETAILS_FIELD = "details"
ORIGINAL_PREFIX = "Original title: "


def clean_csv(path, dry_run=False):
    """Rewrite a single SEBI CSV in place. Returns stats dict."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    stats = {
        "path": path,
        "total": len(rows),
        "already_clean": 0,
        "extracted": 0,
        "no_match": 0,
        "per_pattern": {},
        "max_len_before": 0,
        "max_len_after": 0,
    }

    for r in rows:
        title = r.get(NAME_FIELD, "") or ""
        stats["max_len_before"] = max(stats["max_len_before"], len(title))
        new_name, label = extract_entity_name(title)
        stats["per_pattern"][label] = stats["per_pattern"].get(label, 0) + 1
        if label == "already_clean":
            stats["already_clean"] += 1
            stats["max_len_after"] = max(stats["max_len_after"], len(new_name))
            continue
        if label == "no_match":
            stats["no_match"] += 1
            stats["max_len_after"] = max(stats["max_len_after"], len(new_name))
            continue
        # Extraction success: preserve original in details so downstream
        # tools can still see the full title.
        existing_details = r.get(DETAILS_FIELD, "") or ""
        if ORIGINAL_PREFIX not in existing_details and title and title != new_name:
            prefix = ORIGINAL_PREFIX + title
            r[DETAILS_FIELD] = (
                prefix if not existing_details
                else f"{prefix} | {existing_details}"
            )
        r[NAME_FIELD] = new_name
        stats["extracted"] += 1
        stats["max_len_after"] = max(stats["max_len_after"], len(new_name))

    if not dry_run:
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)

    return stats


def _format_stats(s):
    pct = lambda n: f"{(n / max(1, s['total']) * 100):5.1f}%"
    return (f"{os.path.basename(s['path']):42}  "
            f"total={s['total']:>5}  "
            f"extracted={s['extracted']:>5} ({pct(s['extracted'])})  "
            f"clean={s['already_clean']:>4} ({pct(s['already_clean'])})  "
            f"nomatch={s['no_match']:>4} ({pct(s['no_match'])})  "
            f"max_len {s['max_len_before']} -> {s['max_len_after']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Write changes back to disk")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print stats only; do not modify files")
    ap.add_argument("--file", help="Process only this CSV")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("pick --apply or --dry-run")

    if args.file:
        paths = [args.file]
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = sorted(glob.glob(os.path.join(root, "data", "sebi_*.csv")))
    if not paths:
        print("no CSV files matched")
        sys.exit(1)

    grand = {"total": 0, "extracted": 0, "already_clean": 0, "no_match": 0,
             "per_pattern": {}}
    for p in paths:
        s = clean_csv(p, dry_run=args.dry_run)
        print(_format_stats(s))
        for k in ("total", "extracted", "already_clean", "no_match"):
            grand[k] += s[k]
        for k, v in s["per_pattern"].items():
            grand["per_pattern"][k] = grand["per_pattern"].get(k, 0) + v

    print("-" * 90)
    print(f"GRAND  total={grand['total']}  extracted={grand['extracted']}  "
          f"clean={grand['already_clean']}  nomatch={grand['no_match']}  "
          f"mode={'DRY-RUN' if args.dry_run else 'APPLIED'}")
    print("--- pattern breakdown ---")
    for k, v in sorted(grand["per_pattern"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:35} {v:>6}")


if __name__ == "__main__":
    main()
