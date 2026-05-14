"""
utils/confidence.py

Map an engine's extraction strategy to a confidence label. Used by the
optional 18th CSV column (`confidence`) and by validate_schema.py for
quality reporting.

Confidence levels
-----------------
high    - extracted from a structured table with clear headers
          (html_generic table / pdf_structured / xlsx_attached)
medium  - extracted from blocks or PDF text with ambiguity
          (html_block / pdf_text / api_replay / discovery)
low     - OCR or raw text fallback
          (pdf_ocr / unstructured / raw_text / html_passthrough)

Adding to a CSV
---------------
Engines can opt into the 18th column by appending "confidence" to
their CSV_FIELDS list and calling `from_strategy(strategy)` on each
row. The canonical 17-column schema stays the default; combine.py
silently drops the extra column when merging.
"""

STRATEGY_TO_CONFIDENCE = {
    # high
    "table":             "high",
    "html_generic":      "high",
    "pdf_table":         "high",
    "pdf_structured":    "high",
    "xlsx_attached":     "high",
    "csv_attached":      "high",
    # medium
    "blocks":            "medium",
    "html_block":        "medium",
    "pdf_text":          "medium",
    "discovery":         "medium",
    "api_replay":        "medium",
    "json_records":      "medium",
    "json_path":         "medium",
    # low
    "raw_text":          "low",
    "unstructured":      "low",
    "pdf_ocr":           "low",
    "html_passthrough":  "low",
    "raw":               "low",
}


def from_strategy(strategy):
    """Return 'high' | 'medium' | 'low'. Unknown strategies map to
    'medium' so a forgotten enum doesn't silently downgrade rows."""
    if not strategy:
        return "medium"
    return STRATEGY_TO_CONFIDENCE.get(str(strategy).lower(), "medium")


def from_link_kind(link_kind):
    """Heuristic: many existing CSVs have only a link_kind column. We
    derive confidence from it the same way."""
    if not link_kind:
        return "medium"
    lk = str(link_kind).lower()
    if "ocr" in lk or "unstructured" in lk or "raw" in lk:
        return "low"
    if "block" in lk or "discovery" in lk or "api_" in lk:
        return "medium"
    return "high"


CSV_FIELDS_WITH_CONFIDENCE = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status", "confidence",
]
