"""
scripts/cross_source_dedup.py

Find names that appear on multiple watchlists.

This is NOT a cleanup script — duplicates across sources are valuable
signal. Same person on CBI Most Wanted + NIA Most Wanted + SEBI
Debarred = high-priority match. We surface them as a report.

Algorithm
---------
1. Load master_watchlist.csv.
2. Bucket records by normalized name (lowercase, ASCII-fold,
   strip aliases after '@').
3. For each bucket with names from >=2 distinct (source_agency,
   source_list) tuples, emit one match row per pair.
4. Optionally re-score with difflib.SequenceMatcher to catch
   near-misses across buckets (slow on the full set; we only run
   it inside small buckets to keep cost bounded).

Output
------
reports/cross_source_matches.csv (sorted by name, then # of sources)
plus a top-N summary printed to stdout.

Threshold: similarity >= 0.85 for fuzzy bucket merging.

Usage:
    python -m scripts.cross_source_dedup
    python -m scripts.cross_source_dedup --top 50
"""

import argparse
import csv
import difflib
import os
import re
import sys
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_CSV   = os.path.join(PROJECT_ROOT, "data", "master_watchlist.csv")
REPORTS_DIR  = os.path.join(PROJECT_ROOT, "reports")
OUT_CSV      = os.path.join(REPORTS_DIR, "cross_source_matches.csv")

THRESHOLD = 0.85


def _normalize(name):
    if not name:
        return ""
    n = name.strip().lower()
    # split on alias separators
    n = re.split(r"\s+(?:@|alias|a\.k\.a\.|aka)\s+", n)[0]
    # strip honorifics
    n = re.sub(r"^(mr\.?|mrs\.?|ms\.?|shri|smt\.?|dr\.?|prof\.?)\s+",
               "", n)
    # collapse punctuation/whitespace
    n = re.sub(r"[^a-z0-9ऀ-ॿ\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _load(path):
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            name = (r.get("name") or "").strip()
            if not name or len(name) < 4:
                continue
            rows.append({
                "name":          name,
                "norm":          _normalize(name),
                "source_agency": (r.get("source_agency") or "").strip(),
                "source_list":   (r.get("source_list") or "").strip(),
                "address":       (r.get("address") or "").strip(),
                "details":       (r.get("details") or "").strip(),
            })
    return rows


def find_matches(rows):
    """Return list of {name, sources, addresses, similarity}."""
    by_norm = defaultdict(list)
    for r in rows:
        if r["norm"]:
            by_norm[r["norm"]].append(r)

    matches = []
    for norm, group in by_norm.items():
        sources = {(r["source_agency"], r["source_list"]) for r in group}
        if len(sources) < 2:
            continue
        # Pick the longest name representation as canonical.
        canonical = max(group, key=lambda r: len(r["name"]))["name"]
        addresses = {a for a in (r["address"] for r in group) if a}
        matches.append({
            "name": canonical,
            "norm": norm,
            "n_records": len(group),
            "n_sources": len(sources),
            "sources": " || ".join(
                f"{a}: {l}" for a, l in sorted(sources)),
            "addresses": " || ".join(sorted(addresses))[:300],
            "similarity": "1.00",
        })
    matches.sort(key=lambda m: (-m["n_sources"], -m["n_records"], m["name"]))
    return matches


def fuzzy_merge_pairs(rows, threshold=THRESHOLD, max_pairs=200):
    """A second-pass scan: find near-matches across buckets that exact
    norm missed. Bounded to first max_pairs to keep runtime sane."""
    by_norm = defaultdict(list)
    for r in rows:
        if r["norm"]:
            by_norm[r["norm"]].append(r)
    keys = list(by_norm.keys())
    pairs = []
    for i, k in enumerate(keys):
        for k2 in keys[i+1:i+50]:  # limit window
            sim = difflib.SequenceMatcher(None, k, k2).ratio()
            if sim < threshold:
                continue
            srcs1 = {(r["source_agency"], r["source_list"]) for r in by_norm[k]}
            srcs2 = {(r["source_agency"], r["source_list"]) for r in by_norm[k2]}
            shared = srcs1 | srcs2
            if len(shared) < 2:
                continue
            pairs.append({
                "name_a": by_norm[k][0]["name"],
                "name_b": by_norm[k2][0]["name"],
                "similarity": round(sim, 3),
                "sources_combined": " || ".join(
                    f"{a}: {l}" for a, l in sorted(shared)),
            })
            if len(pairs) >= max_pairs:
                return pairs
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--no-fuzzy", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(MASTER_CSV):
        print(f"master_watchlist.csv not found at {MASTER_CSV}",
              file=sys.stderr)
        sys.exit(2)

    rows = _load(MASTER_CSV)
    print(f"Loaded {len(rows):,} records from master_watchlist.csv")

    matches = find_matches(rows)
    print(f"Cross-source matches (exact normalized name): {len(matches)}")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "n_sources", "n_records",
                                          "sources", "addresses",
                                          "similarity", "norm"])
        w.writeheader()
        for m in matches:
            w.writerow(m)

    if not args.no_fuzzy:
        fuzzy = fuzzy_merge_pairs(rows)
        print(f"Fuzzy near-matches (sim>={THRESHOLD}, sample): {len(fuzzy)}")

    print(f"\nFull report: {OUT_CSV}\n")
    print(f"{'name':<50}  {'srcs':>4}  {'recs':>4}  sources")
    print("-" * 120)
    for m in matches[:args.top]:
        print(f"{m['name'][:50]:<50}  {m['n_sources']:>4}  "
              f"{m['n_records']:>4}  {m['sources'][:70]}")


if __name__ == "__main__":
    main()
