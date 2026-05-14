"""
Add `failure_reason` field to every non-(active/completed/skipped) source
in sources.json. Classification is derived from existing fields:

  status=url_not_found  -> data_not_published (or dead_url if notes mention 404/gone)
  status=js             -> js_shell  (or js_no_api if notes mention 'browser yields')
  status=restricted     -> login_required
  status=dead           -> dead_url
  status=failed         -> infer from notes (network/garbage/empty/...)

Idempotent: re-running won't duplicate or overwrite already-classified
entries unless --force is passed.
"""

import argparse
import json
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")

REASONS = {
    "js_shell", "js_no_api", "login_required", "network_error",
    "empty_page", "pdf_scan_failed", "workflow_needed",
    "data_not_published", "dead_url", "duplicate",
}


def classify(s):
    status = s.get("status", "")
    notes  = (s.get("notes") or "").lower()

    if status in ("active", "skipped"):
        return None  # don't tag

    if status == "url_not_found":
        if "404" in notes or "nxdomain" in notes or "moved" in notes or "gone" in notes:
            return "dead_url"
        return "data_not_published"

    if status == "js":
        if "browser yields" in notes or "browser-render" in notes or "browser render" in notes:
            return "js_no_api"
        return "js_shell"

    if status == "restricted":
        if "login" in notes or "subscription" in notes or "captcha" in notes \
           or "search-only" in notes or "portal" in notes:
            return "login_required"
        return "login_required"

    if status == "dead":
        return "dead_url"

    if status == "failed":
        if "network" in notes or "unreachable" in notes or "timeout" in notes:
            return "network_error"
        if "garbage" in notes or "duplicate" in notes:
            return "empty_page"
        if "chrome_content_not_data" in notes or "chrome content" in notes:
            return "empty_page"
        if "ocr" in notes and "fail" in notes:
            return "pdf_scan_failed"
        # Default for failed without strong signal
        return "empty_page"

    if status == "partial":
        # don't tag partial — these are still active
        return None

    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing failure_reason values")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        doc = json.load(f)

    updated = 0
    counts = {}
    for s in doc["sources"]:
        reason = classify(s)
        if reason is None:
            continue
        if "failure_reason" in s and not args.force:
            counts[reason] = counts.get(reason, 0) + 1
            continue
        s["failure_reason"] = reason
        updated += 1
        counts[reason] = counts.get(reason, 0) + 1

    if not args.dry_run:
        with open(SOURCES_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)

    print(f"Updated {updated} entries in {SOURCES_PATH}")
    print("Tagged distribution:")
    for r, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {r:24s} {n:>4d}")


if __name__ == "__main__":
    main()
