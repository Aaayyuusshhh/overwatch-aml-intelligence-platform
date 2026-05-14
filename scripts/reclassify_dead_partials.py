"""Bulk-update sources.json: change status + failure_reason for 23
dead partial sources, per the triage outcome. Idempotent: re-running
on already-changed entries is a no-op."""
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")

# (ppt_number, new_status, failure_reason)
RECLASS = [
    # MCA portal — all 403
    (40,  "restricted", "MCA V3 portal returns HTTP 403, requires authenticated access"),
    (41,  "restricted", "MCA V3 portal returns HTTP 403, requires authenticated access"),
    (48,  "restricted", "MCA V3 portal returns HTTP 403, requires authenticated access"),
    (49,  "restricted", "MCA V3 portal returns HTTP 403, requires authenticated access"),
    (52,  "restricted", "MCA V3 portal returns HTTP 403, requires authenticated access"),
    (53,  "restricted", "MCA V3 portal returns HTTP 403, requires authenticated access"),
    (55,  "restricted", "MCA V3 portal returns HTTP 403, requires authenticated access"),
    # MSEI ASP.NET errors — all 563 bytes
    (170, "dead_url",   "MSEI deprecated list pages, 563-byte ASP.NET error redirect"),
    (171, "dead_url",   "MSEI deprecated list pages, 563-byte ASP.NET error redirect"),
    (172, "dead_url",   "MSEI deprecated list pages, 563-byte ASP.NET error redirect"),
    (173, "dead_url",   "MSEI deprecated list pages, 563-byte ASP.NET error redirect"),
    (174, "dead_url",   "MSEI deprecated list pages, 563-byte ASP.NET error redirect"),
    (175, "dead_url",   "MSEI deprecated list pages, 563-byte ASP.NET error redirect"),
    (236, "dead_url",   "MSEI deprecated list pages, 563-byte ASP.NET error redirect"),
    (242, "dead_url",   "MSEI deprecated list pages, 563-byte ASP.NET error redirect"),
    # Others
    (17,  "js",         "SPA renders 2-row placeholder table, real data behind JS framework"),
    (24,  "js",         "ED site is React SPA, no static content"),
    (26,  "dead_url",   "246-byte body, page removed"),
    (75,  "network_error", "Connection timeout, retry next run"),
    (76,  "dead_url",   "Generic MSJE template, data page removed"),
    (77,  "dead_url",   "Generic MSJE template, data page removed"),
    (180, "restricted", "HTTP 403, MCX bot-blocking notice-board"),
    (223, "dead_url",   "Redirects to PIB landing, original URL gone"),
]


def main():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    by_ppt = {s.get("ppt_number"): s for s in data["sources"]}

    changed = 0
    for ppt, new_status, reason in RECLASS:
        s = by_ppt.get(ppt)
        if s is None:
            print(f"  WARN: ppt #{ppt} not found in sources.json")
            continue
        old = s.get("status")
        s["status"] = new_status
        s["failure_reason"] = reason
        # Disable change_detection for terminal states.
        if new_status in ("dead_url", "restricted"):
            s["change_detection"] = False
        changed += 1
        print(f"  #{ppt:>3}  {old:<12} → {new_status:<12}  {reason[:60]}")

    with open(SOURCES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nUpdated {changed} sources in sources.json")

    # Status distribution after change
    from collections import Counter
    by_status = Counter(s.get("status") for s in data["sources"])
    print("\nsources.json status distribution (after):")
    for k, v in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {k:<18} {v:>4}")


if __name__ == "__main__":
    main()
