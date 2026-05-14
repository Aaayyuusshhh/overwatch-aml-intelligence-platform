"""
engines/config_engines/validator.py — post-extraction data-quality
gate. Run on the list[dict] that an engine produces before it gets
written to disk; returns (ok, issues_list).
"""

import re
from typing import List, Tuple

HTML_TAG_RE = re.compile(r"<[a-zA-Z/!?][^>]*>")
DIGIT_ONLY_RE = re.compile(r"^\s*[\d.,\s-]+$")


def validate_output(records: list, config: dict) -> Tuple[bool, List[str]]:
    issues = []
    v = config.get("validation", {}) or {}
    min_records   = v.get("min_records", 1)
    required      = v.get("required_fields", ["name"])
    max_empty_pct = float(v.get("max_empty_name_pct", 5))
    allow_digit_names = bool(v.get("allow_digit_only_names", False))

    n = len(records or [])
    if n < min_records:
        issues.append(f"Expected min {min_records} records, got {n}")

    if not records:
        return False, issues

    # Required-field check
    missing_counts = {k: 0 for k in required}
    empty_name = 0
    html_in_name = 0
    digit_name = 0
    for r in records:
        for k in required:
            if not (r.get(k) and str(r.get(k)).strip()):
                missing_counts[k] += 1
        nm = (r.get("name") or "").strip()
        if not nm:
            empty_name += 1
            continue
        if HTML_TAG_RE.search(nm):
            html_in_name += 1
        if not allow_digit_names and DIGIT_ONLY_RE.match(nm):
            digit_name += 1

    for k, c in missing_counts.items():
        if c:
            issues.append(f"{c} records missing required field '{k}'")

    pct_empty = (empty_name / n) * 100
    if pct_empty > max_empty_pct:
        issues.append(
            f"{pct_empty:.1f}% empty names exceeds threshold "
            f"{max_empty_pct}%"
        )

    if html_in_name:
        issues.append(f"{html_in_name} names contain HTML tags")
    if digit_name:
        issues.append(f"{digit_name} names are digit-only "
                       "(set allow_digit_only_names=true if intended)")

    return (len(issues) == 0), issues
