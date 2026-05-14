"""One-off: extract real entity names from `details` for FIU #28 and
REC #156 CSVs, swap into `name` column. Keeps html_generic link_kind
since the data was originally extracted by the generic engine — we're
just relabeling columns post-hoc rather than re-scraping."""
import csv
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _process(in_path, name_extractor):
    rows = []
    with open(in_path, "r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        flds = rdr.fieldnames
        for r in rdr:
            new_name = name_extractor(r)
            if new_name:
                r["name"] = new_name
                # case_unit retains the serial number which was previously
                # in name; that's fine.
            rows.append(r)
    with open(in_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flds)
        w.writeheader()
        w.writerows(rows)
    print(f"[{os.path.basename(in_path)}] rewrote {len(rows)} rows")


def fiu_extract(r):
    """details = 'Date: ... | Description: <Bank Name>, Order-in-Original ...'
    Pull out the entity name as the chunk before the first comma after
    'Description:'."""
    d = r.get("details") or ""
    m = re.search(r"Description:\s*([^|]+)", d)
    if not m:
        return None
    desc = m.group(1).strip().rstrip(",").strip()
    # Trim trailing ', Order-in-Original ...' or ', M-13/...'
    name = re.split(r",\s*(?:Order-in-Original|Order-in-original|S/o|D/o|R/o)",
                    desc, maxsplit=1)[0]
    name = name.split(" Order-in-Original")[0]
    return name.strip(", ").strip()


def rec_extract(r):
    """details = 'कंपनी का नाम: <name> | कंपनी का पता: ...'"""
    d = r.get("details") or ""
    m = re.search(r"कंपनी का नाम:\s*([^|]+)", d)
    if not m:
        return None
    return m.group(1).strip().rstrip(",").strip()


_process(os.path.join(PROJECT_ROOT, "data", "fiu_judgements_28.csv"),
         fiu_extract)
_process(os.path.join(PROJECT_ROOT, "data", "rec_list_of_banned_firms_156.csv"),
         rec_extract)
