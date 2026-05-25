#!/usr/bin/env python3
"""Fix blank period_from/period_to in rbi_odi_investments table.

Many older Excel files (2011-2014) have different sheet naming conventions
and the parser couldn't extract the From/To dates. This script:
1. Reads all distinct (period, excel_filename) combos with blank period_from
2. Infers the month/year from the period name or filename
3. Updates the DB rows with correct period_from/period_to
"""
import re
import sys
import calendar

import psycopg2

LOCAL = dict(host="localhost", user="aayush", password="aayush123", dbname="risk_pipeline")
RDS = dict(
    host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
    user="aayush", password="Aaayyuusshhh", dbname="risk_pipeline",
    connect_timeout=30,
)

MONTH_MAP = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
    'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}


def infer_period(period_text, filename):
    """Try to infer (month, year) from period text or filename.
    Returns (period_from, period_to) as DD/MM/YYYY strings, or (None, None)."""

    combined = f"{period_text} {filename}".lower()

    # Pattern 1: "Month YYYY" or "Month_YYYY" or "Month, YYYY"
    for month_name, month_num in MONTH_MAP.items():
        # Look for month name followed by 4-digit year
        pattern = rf'\b{month_name}[_,\s]*(\d{{4}})\b'
        m = re.search(pattern, combined)
        if m:
            year = int(m.group(1))
            if 2005 <= year <= 2030:
                last_day = calendar.monthrange(year, month_num)[1]
                return f"01/{month_num:02d}/{year}", f"{last_day}/{month_num:02d}/{year}"

    # Pattern 2: "MMYYYY" or "MMYY" in filename (e.g., OFDI0913.xls = Sep 2013)
    m = re.search(r'(\d{2})(\d{2})(\d{2,4})\.xls', filename.lower())
    if m:
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        mm_int = int(mm)
        if 1 <= mm_int <= 12:
            year = int(yy)
            if year < 100:
                year += 2000
            if 2005 <= year <= 2030:
                last_day = calendar.monthrange(year, mm_int)[1]
                return f"01/{mm_int:02d}/{year}", f"{last_day}/{mm_int:02d}/{year}"

    # Pattern 3: filename contains month abbreviation + year digits
    # e.g., PR1638OFDI1117_NOV.xls -> Nov 2017 (already handled but check again)
    for month_name, month_num in MONTH_MAP.items():
        if month_name in combined:
            # Find any 4-digit year nearby
            years = re.findall(r'20\d{2}', combined)
            if years:
                year = int(years[-1])  # Take the last one
                last_day = calendar.monthrange(year, month_num)[1]
                return f"01/{month_num:02d}/{year}", f"{last_day}/{month_num:02d}/{year}"
            # Try 2-digit year
            years_2d = re.findall(r'(\d{2})(?:\.xls|_)', combined)
            for y2 in years_2d:
                year = int(y2) + 2000
                if 2005 <= year <= 2030:
                    last_day = calendar.monthrange(year, month_num)[1]
                    return f"01/{month_num:02d}/{year}", f"{last_day}/{month_num:02d}/{year}"

    # Pattern 4: "DDMMYYYY" in filename (e.g., OFDI14092023 = 14 Sep 2023)
    m = re.search(r'(\d{2})(\d{2})(20\d{2})', filename)
    if m:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12:
            last_day = calendar.monthrange(yyyy, mm)[1]
            # The date in filename is release date; actual period is previous month
            if mm == 1:
                pm, py = 12, yyyy - 1
            else:
                pm, py = mm - 1, yyyy
            last_day_prev = calendar.monthrange(py, pm)[1]
            return f"01/{pm:02d}/{py}", f"{last_day_prev}/{pm:02d}/{py}"

    # Pattern 5: R042_ sheets - look at filename for year hint
    m = re.search(r'(20\d{2})', filename)
    if m:
        # We know the year but not the month from R042 sheets
        # Try to get month from the period text if it has any hint
        return None, None

    return None, None


def fix_periods(label, conn_kwargs):
    conn = psycopg2.connect(**conn_kwargs)
    cur = conn.cursor()

    # Get all distinct combos with blank period_from
    cur.execute("""
        SELECT DISTINCT period, excel_filename, COUNT(*) as cnt
        FROM rbi_odi_investments
        WHERE period_from IS NULL OR period_from = ''
        GROUP BY period, excel_filename
        ORDER BY period;
    """)
    blanks = cur.fetchall()

    if not blanks:
        print(f"[{label}] No blank period_from rows found!")
        conn.close()
        return

    print(f"[{label}] Found {len(blanks)} distinct (period, filename) combos with blank periods:")

    fixed = 0
    skipped = 0
    for period, filename, cnt in blanks:
        pf, pt = infer_period(period or '', filename or '')
        if pf and pt:
            cur.execute("""
                UPDATE rbi_odi_investments
                SET period_from = %s, period_to = %s
                WHERE (period_from IS NULL OR period_from = '')
                  AND period = %s AND excel_filename = %s;
            """, (pf, pt, period, filename))
            updated = cur.rowcount
            print(f"  ✓ {period:50s} ({filename:40s}) -> {pf} to {pt}  ({updated} rows)")
            fixed += updated
        else:
            print(f"  ✗ {period:50s} ({filename:40s}) -> CANNOT INFER  ({cnt} rows)")
            skipped += cnt

    conn.commit()

    # Check remaining blanks
    cur.execute("SELECT COUNT(*) FROM rbi_odi_investments WHERE period_from IS NULL OR period_from = '';")
    remaining = cur.fetchone()[0]

    # Also show the full year/month coverage now
    cur.execute("""
        SELECT SUBSTRING(period_from, 7, 4) AS year,
               COUNT(DISTINCT SUBSTRING(period_from, 4, 2)) AS months,
               COUNT(*) AS rows
        FROM rbi_odi_investments
        WHERE period_from IS NOT NULL AND period_from != ''
        GROUP BY SUBSTRING(period_from, 7, 4)
        ORDER BY year;
    """)
    print(f"\n[{label}] Coverage after fix:")
    for year, months, rows in cur.fetchall():
        status = "✅" if months >= 10 else "⚠️" if months >= 6 else "❌"
        print(f"  {year}: {months:2d} months, {rows:>6,} rows {status}")

    print(f"\n[{label}] Fixed: {fixed:,} rows | Still blank: {remaining:,} rows | Skipped: {skipped:,}")
    conn.close()


if __name__ == "__main__":
    targets = sys.argv[1:] or ["local", "rds"]
    if "local" in targets:
        fix_periods("local", LOCAL)
    if "rds" in targets:
        fix_periods("RDS  ", RDS)
