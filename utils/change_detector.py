"""
Source-side change detection via SHA-256 hashing.

Per ARCHITECTURE.md §6.4 / PRD §6.5. Two-tier strategy to avoid
false positives from dynamic page chrome (CSRF tokens, session IDs,
timestamps):

  1. If sources.json provides 'change_detection_selector', hash the
     concatenated outer HTML of every element matching that selector.
     This naturally excludes <head> / <script> / chrome from the hash.
  2. Otherwise hash the full body, after stripping known dynamic
     patterns (currently: csrf-token meta tag).

Diagnosed false positives on CBI / NIA pages came from a single
rotating <meta name="csrf-token" content="..."> in <head>; both tiers
above defeat it.

PDF sources skip change detection entirely (their URL itself encodes
the gazette date, so hashing is meaningless).
"""

import hashlib
import os
import re

from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS_DIR = os.path.join(PROJECT_ROOT, "snapshots")

# Patterns we strip when hashing the full body (selector-based hashing
# bypasses these but the regex still runs as defence-in-depth).
DYNAMIC_PATTERNS = [
    # Drupal / CBI csrf rotates every response.
    re.compile(r'<meta\s+name=["\']csrf-token["\']\s+content=["\'][^"\']*["\']\s*/?>',
               re.IGNORECASE),
]


def _hash_path(source_id):
    return os.path.join(SNAPSHOTS_DIR, f"{source_id}.hash")


def _read_hash(source_id):
    p = _hash_path(source_id)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return f.read().strip()


def _write_hash(source_id, digest):
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    with open(_hash_path(source_id), "w", encoding="utf-8") as f:
        f.write(digest)


def _strip_dynamic(text):
    for pat in DYNAMIC_PATTERNS:
        text = pat.sub("", text)
    return text


def _compute_payload(page, selector):
    """Return the bytes to hash. Selector-subtree first, body+strip second."""
    if selector:
        elements = page.find_all(selector)
        if elements:
            joined = "".join(str(e.html_content) for e in elements)
            return joined.encode("utf-8")
    body = page.body if hasattr(page, "body") else page.html_content
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return _strip_dynamic(body).encode("utf-8")


def check_for_change(source_id, url, source_type, selector=None):
    """Return 'skipped' / 'first_run' / 'unchanged' / 'changed'."""
    if source_type == "pdf" or not url:
        return "skipped"

    try:
        page = Fetcher.get(url)
    except Exception:
        return "skipped"

    new_hash = hashlib.sha256(_compute_payload(page, selector)).hexdigest()
    old_hash = _read_hash(source_id)

    if old_hash is None:
        result = "first_run"
    elif old_hash == new_hash:
        result = "unchanged"
    else:
        result = "changed"

    _write_hash(source_id, new_hash)
    return result
