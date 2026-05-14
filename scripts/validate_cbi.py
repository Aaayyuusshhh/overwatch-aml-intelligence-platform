"""
Validation report for a CBI scraper output CSV.

Usage:
  python scripts/validate_cbi.py                                # default: announced rewards
  python scripts/validate_cbi.py data/cbi_yellow_notices.csv    # any other CBI CSV

Read-only. Loads the CSV with pandas and prints a labeled diagnostic
section for each data-quality concern:

  1. Shape + per-column null counts
  2. Duplicate-record check (canonical key per group)
  3. date_of_birth out-of-range (future or pre-1900) or unparseable
  4. has_document / document_url consistency
  5. Distributions: gender, reward_amount format patterns,
                    father_name "missing" variants
  6. Suspect case_unit rows (length > 60 chars)
  7. link_kind distribution (and presence of interpol_notice_id where expected)
  8. enrichment_status distribution (must be one of none/partial/full)
"""

import os
import re
import sys
from datetime import datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV = os.path.join(PROJECT_ROOT, "data", "cbi_announced_rewards.csv")

# Strings that downstream code/users sometimes use to mean "missing".
MISSING_VARIANTS = {"", "-", "--", "N/A", "n/a", "NA", "na", "Unknown", "unknown", "None", "none"}

CASE_UNIT_MAX_LEN = 60   # heuristic: anything longer is likely contaminated


# ---------- helpers ----------
def header(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def reward_pattern(value):
    """Bucket a reward_amount string into a coarse format label."""
    if pd.isna(value) or str(value).strip() == "":
        return "empty"
    s = str(value).strip()
    if s in {"-", "--"}:
        return f"dash ({s!r})"
    if re.fullmatch(r"\d+", s):
        return "plain integer"
    if re.search(r"Rs\.?\s*[\d,]+", s):
        return "Rs-prefixed"
    if re.search(r"[\d,]+/-", s):
        return "trailing /-"
    return "other"


# ---------- checks ----------
def check_shape_and_nulls(df):
    header("1. SHAPE & PER-COLUMN NULL COUNTS")
    print(f"Rows: {len(df)}")
    print(f"Columns: {len(df.columns)}")
    print("\nNulls per column (pandas NaN = empty CSV cell):")
    nulls = df.isna().sum()
    for col, n in nulls.items():
        print(f"  {col:<20} {n}")


def check_duplicates(df):
    header("2. DUPLICATE RECORD CHECK")
    # Two-tier dedup. CBI publishes the same Interpol notice under multiple
    # URL prefixes (e.g., /How-we-work/Notices/View-Red-Notices vs the
    # /en/.../Red-Notices/View-Red-Notices locale form), so URL-string
    # equality undercounts duplicates for interpol-notice rows. Use
    # interpol_notice_id as the canonical key for those, and fall back to
    # detail_page_url for everything else.

    if "link_kind" in df.columns and "interpol_notice_id" in df.columns:
        link_kind = df["link_kind"].fillna("")
        notice_id = df["interpol_notice_id"].fillna("").astype(str).str.strip()
        is_interpol = (link_kind == "interpol_notice") & (notice_id != "")
    else:
        is_interpol = pd.Series(False, index=df.index)

    group_a = df[is_interpol]                    # interpol_notice + notice_id
    group_b = df[~is_interpol]                   # everything else

    # Group A: dedup by interpol_notice_id
    dupes_a = group_a[group_a.duplicated(subset=["interpol_notice_id"], keep=False)]

    # Group B: dedup by detail_page_url, excluding empty values
    url_b = group_b["detail_page_url"].fillna("").astype(str).str.strip()
    populated_b = group_b[url_b != ""]
    skipped_b = len(group_b) - len(populated_b)
    dupes_b = populated_b[populated_b.duplicated(subset=["detail_page_url"], keep=False)]

    print(f"Group A: interpol_notice rows with notice_id  ->  {len(group_a)} rows")
    print(f"  Duplicates by interpol_notice_id:          {len(dupes_a)}")
    print(f"Group B: all other rows                       ->  {len(group_b)} rows")
    print(f"  Empty detail_page_url (excluded from check): {skipped_b}")
    print(f"  Duplicates by detail_page_url:             {len(dupes_b)}")

    if not dupes_a.empty:
        print("\nGroup A duplicates (same interpol_notice_id):")
        print(dupes_a.sort_values("interpol_notice_id")[
            ["name", "interpol_notice_id", "detail_page_url"]
        ].to_string(index=False))
    if not dupes_b.empty:
        print("\nGroup B duplicates (same detail_page_url):")
        print(dupes_b.sort_values("detail_page_url")[
            ["name", "detail_page_url"]
        ].to_string(index=False))


def check_dates(df):
    header("3. date_of_birth OUT-OF-RANGE OR UNPARSEABLE")
    parsed = pd.to_datetime(df["date_of_birth"], errors="coerce")
    today = pd.Timestamp(datetime.now().date())
    floor = pd.Timestamp("1900-01-01")

    unparseable_mask = parsed.isna() & df["date_of_birth"].notna() & (df["date_of_birth"].astype(str).str.strip() != "")
    future_mask = parsed.notna() & (parsed > today)
    too_old_mask = parsed.notna() & (parsed < floor)

    print(f"Unparseable (non-empty but not a date): {unparseable_mask.sum()}")
    print(f"In the future (> {today.date()}):       {future_mask.sum()}")
    print(f"Before 1900:                            {too_old_mask.sum()}")

    bad = df[unparseable_mask | future_mask | too_old_mask][
        ["name", "date_of_birth", "detail_page_url"]
    ]
    if not bad.empty:
        print("\nOffending rows:")
        print(bad.to_string(index=False))

    # Surface DOBs that repeat across unrelated rows — likely a fallback default.
    repeats = (
        df[parsed.notna()]
        .groupby("date_of_birth")
        .size()
        .reset_index(name="count")
        .query("count > 1")
        .sort_values("count", ascending=False)
    )
    if not repeats.empty:
        print("\nRepeated DOB values (possible fallback defaults):")
        print(repeats.to_string(index=False))


def check_document_consistency(df):
    header("4. has_document / document_url CONSISTENCY")
    has_yes = df["has_document"].astype(str).str.strip().str.lower() == "yes"
    url_empty = df["document_url"].isna() | (df["document_url"].astype(str).str.strip() == "")

    yes_but_empty = df[has_yes & url_empty]
    no_but_filled = df[~has_yes & ~url_empty]

    print(f"has_document='Yes' but document_url empty:   {len(yes_but_empty)}")
    print(f"has_document!='Yes' but document_url filled: {len(no_but_filled)}")

    if not yes_but_empty.empty:
        print("\nYes-but-empty rows:")
        print(yes_but_empty[["name", "has_document", "document_url"]].to_string(index=False))
    if not no_but_filled.empty:
        print("\nNo-but-filled rows:")
        print(no_but_filled[["name", "has_document", "document_url"]].to_string(index=False))


def check_distributions(df):
    header("5. DISTRIBUTIONS (gender / reward_amount / father_name)")

    print("\n-- gender unique values --")
    print(df["gender"].fillna("<NaN>").value_counts(dropna=False).to_string())

    print("\n-- reward_amount format patterns --")
    patterns = df["reward_amount"].apply(reward_pattern).value_counts()
    print(patterns.to_string())
    print("\n   raw unique reward_amount values:")
    for v in sorted(df["reward_amount"].fillna("").astype(str).unique()):
        print(f"     {v!r}")

    print("\n-- father_name 'missing' variants found --")
    fn = df["father_name"].fillna("").astype(str).str.strip()
    missing_hits = fn[fn.isin(MISSING_VARIANTS)]
    print(missing_hits.value_counts(dropna=False).to_string() if not missing_hits.empty else "(none)")


def check_case_unit(df):
    header(f"6. SUSPECT case_unit ROWS (length > {CASE_UNIT_MAX_LEN})")
    cu = df["case_unit"].fillna("").astype(str)
    long_mask = cu.str.len() > CASE_UNIT_MAX_LEN
    print(f"Rows over the threshold: {long_mask.sum()}")
    if long_mask.any():
        offenders = df.loc[long_mask, ["name", "case_unit", "detail_page_url"]].copy()
        offenders["case_unit_len"] = cu[long_mask].str.len()
        # Show the full case_unit so contamination is visible.
        with pd.option_context("display.max_colwidth", None, "display.width", 200):
            print(offenders.to_string(index=False))


def check_enrichment_status(df):
    header("8. enrichment_status DISTRIBUTION")
    if "enrichment_status" not in df.columns:
        print("(column not present)")
        return
    counts = df["enrichment_status"].fillna("").value_counts(dropna=False)
    print(counts.to_string())
    allowed = {"none", "partial", "full"}
    bad = df[~df["enrichment_status"].fillna("").isin(allowed)]
    print(f"\nRows with disallowed enrichment_status: {len(bad)}")
    if not bad.empty:
        print(bad[["name", "enrichment_status", "detail_page_url"]].to_string(index=False))


def check_link_kind(df):
    header("7. link_kind DISTRIBUTION")
    if "link_kind" not in df.columns:
        print("(column not present)")
        return
    counts = df["link_kind"].fillna("").value_counts(dropna=False)
    print(counts.to_string())

    # Cross-check: interpol_notice rows should have a notice id; cbi_document rows should not.
    if "interpol_notice_id" in df.columns:
        notice_id = df["interpol_notice_id"].fillna("").astype(str).str.strip()
        is_interpol = df["link_kind"].fillna("") == "interpol_notice"
        is_doc = df["link_kind"].fillna("") == "cbi_document"
        interpol_missing_id = (is_interpol & (notice_id == "")).sum()
        doc_with_id = (is_doc & (notice_id != "")).sum()
        print(f"\ninterpol_notice rows missing interpol_notice_id: {interpol_missing_id}")
        print(f"cbi_document rows that have a notice_id (should be 0): {doc_with_id}")


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    if not os.path.isabs(csv_path):
        csv_path = os.path.join(PROJECT_ROOT, csv_path)

    if not os.path.exists(csv_path):
        print(f"CSV not found at {csv_path}")
        return

    df = pd.read_csv(csv_path, dtype=str, keep_default_na=True)
    print(f"Loaded {csv_path}")

    check_shape_and_nulls(df)
    check_duplicates(df)
    check_dates(df)
    check_document_consistency(df)
    check_distributions(df)
    check_case_unit(df)
    check_link_kind(df)
    check_enrichment_status(df)

    print("\n" + "=" * 70)
    print("Validation complete.")
    print("=" * 70)


if __name__ == "__main__":
    main()
