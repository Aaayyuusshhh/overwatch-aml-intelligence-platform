"""Triage all 'partial'-tier sources. Read-only inventory: lists each
partial, looks up CSV row count, peeks at first/last rows, and emits a
machine-friendly table for Steps 2-5.

Does NOT modify sources.json, CSVs, or the DB.
"""
import csv
import json
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")
SCRAPERS_DIR = os.path.join(PROJECT_ROOT, "scrapers")
TRACKER_PATH = os.path.join(PROJECT_ROOT, "project_status.xlsx")

NAV_REGEX = re.compile(
    r"^(home|menu|click|login|copyright|skip|search|back|next|previous|page|"
    r"continue|view|read more|download|submit)$", re.I)


def load_tracker_status():
    """Return {ppt_number: tracker_status_string}."""
    import openpyxl
    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True)
    ws = wb.active
    hdr = [c.value for c in next(ws.rows)]
    i_ppt    = hdr.index("ppt_number")
    i_status = hdr.index("status")
    i_records = hdr.index("records")
    out = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        ppt = row[i_ppt]
        if ppt is None:
            continue
        out[ppt] = (row[i_status], row[i_records])
    return out


def csv_info(sid):
    """Return (exists, rows, first_name, link_kind, garbage_count)."""
    path = os.path.join(DATA_DIR, f"{sid}.csv")
    if not os.path.exists(path):
        return False, 0, "", "", 0
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        rows = list(rdr)
    if not rows:
        return True, 0, "", "", 0
    first_name = (rows[0].get("name") or "").strip()
    link_kind  = (rows[0].get("link_kind") or "").strip()
    garbage = sum(1 for r in rows
                  if not (r.get("name") or "").strip()
                  or NAV_REGEX.match((r.get("name") or "").strip()))
    return True, len(rows), first_name, link_kind, garbage


def has_scraper(scraper_name):
    if not scraper_name:
        return False
    return os.path.exists(os.path.join(SCRAPERS_DIR, scraper_name))


def main():
    with open(os.path.join(PROJECT_ROOT, "sources.json"),
              "r", encoding="utf-8") as f:
        sources = json.load(f)["sources"]
    tracker = load_tracker_status()

    partials = []
    for s in sources:
        ppt = s.get("ppt_number")
        if ppt is None:
            continue
        t_status, t_records = tracker.get(ppt, (None, None))
        if t_status != "partial":
            continue
        exists, n, first_name, lk, garbage = csv_info(s["id"])
        partials.append({
            "ppt":          ppt,
            "agency":       s.get("agency", ""),
            "list_name":    s.get("list_name", ""),
            "url":          s.get("url") or "",
            "type":         s.get("type", ""),
            "id":           s["id"],
            "scraper":      s.get("scraper") or "",
            "scraper_exists": has_scraper(s.get("scraper")),
            "failure_reason": s.get("failure_reason", ""),
            "notes":        (s.get("notes") or "")[:80],
            "csv_exists":   exists,
            "csv_rows":     n,
            "first_name":   first_name[:60],
            "link_kind":    lk,
            "garbage":      garbage,
        })

    # Sort by CSV rows DESC (most data first).
    partials.sort(key=lambda p: -p["csv_rows"])

    print(f"### STEP 1 — {len(partials)} partial sources\n")
    print(f"{'ppt':>4} {'rows':>5} {'gb':>3}  "
          f"{'agency':<35} {'list_name':<45} "
          f"{'type':<5} {'lk':<14} scrap?  notes")
    print("-" * 180)
    for p in partials:
        scr = "✓" if p["scraper"] else "—"
        print(f"{p['ppt']:>4} {p['csv_rows']:>5} {p['garbage']:>3}  "
              f"{p['agency'][:35]:<35} "
              f"{p['list_name'][:45]:<45} "
              f"{p['type']:<5} {p['link_kind']:<14} {scr:<6}  "
              f"{p['notes'][:50]}")

    # Tier classification
    tier_a, tier_b, tier_c, tier_d = [], [], [], []
    for p in partials:
        rows = p["csv_rows"]
        lk = p["link_kind"]
        garbage_pct = (p["garbage"] / rows * 100) if rows else 0
        # D first — overrides everything
        if not p["csv_exists"]:
            tier_d.append((p, "no CSV present"))
            continue
        if rows == 0:
            tier_d.append((p, "CSV exists but empty"))
            continue
        # A: 5+ structured rows, low garbage
        if rows >= 5 and lk not in ("unstructured", "raw_text") and garbage_pct < 20:
            tier_a.append((p, "5+ structured rows, low garbage — candidate for immediate flip"))
            continue
        # B: 1-4 rows OR 5+ but with unstructured link_kind
        if rows >= 1 and rows <= 4:
            tier_b.append((p, f"only {rows} row(s) — likely partial extraction"))
            continue
        if rows >= 5 and lk in ("unstructured", "raw_text"):
            tier_b.append((p, "≥5 rows but link_kind=unstructured/raw_text"))
            continue
        # C: anything else
        tier_c.append((p, "fallthrough"))

    print(f"\n\n### STEP 2 — tier categorization")
    for name, tier in (("A — Almost done", tier_a),
                       ("B — Needs small fix", tier_b),
                       ("C — Needs rework", tier_c),
                       ("D — Probably unscrappable", tier_d)):
        print(f"\n--- TIER {name} ({len(tier)}) ---")
        for p, reason in tier:
            print(f"  #{p['ppt']:>3}  rows={p['csv_rows']:>4}  lk={p['link_kind']:<14}  "
                  f"{p['agency'][:28]:<28}  {p['list_name'][:45]:<45}  → {reason}")

    # Persist tier metadata for the next step
    tiers = {"A": [p["ppt"] for p, _ in tier_a],
             "B": [p["ppt"] for p, _ in tier_b],
             "C": [p["ppt"] for p, _ in tier_c],
             "D": [p["ppt"] for p, _ in tier_d],
             "partials": partials}
    with open(os.path.join(PROJECT_ROOT, "logs", "_triage_tiers.json"),
              "w", encoding="utf-8") as f:
        json.dump(tiers, f, indent=2, default=str)
    print(f"\nwrote tier inventory: logs/_triage_tiers.json")


if __name__ == "__main__":
    main()
