"""
engines/config_engines/csv_writer.py — write config-engine output
into the project's canonical 17-column CSV schema at data/<source_id>.csv.
"""

import csv
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _safe_filename(source_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", source_id)


def write_records(records: list, source_id: str,
                   out_dir: str = DATA_DIR) -> str:
    """Write `records` to {out_dir}/{source_id}.csv in canonical schema.

    Missing schema columns are filled with empty strings. Returns the
    output path."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{_safe_filename(source_id)}.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS,
                                  extrasaction="ignore")
        writer.writeheader()
        for r in records or []:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k))
                              for k in CSV_FIELDS})
    return path
