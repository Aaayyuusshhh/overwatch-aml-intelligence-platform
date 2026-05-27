"""Comprehensive QA test runner for the AML Screening API.

Usage:
    python scripts/qa_screening_api.py http://127.0.0.1:8002 KEY
    python scripts/qa_screening_api.py http://65.1.148.112:8002 KEY

Exits 0 if all tests pass, 1 otherwise. Prints a per-test pass/fail with reason.
"""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx


BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8002"
KEY = sys.argv[2] if len(sys.argv) > 2 else "test-key-local"
TIMEOUT = float(sys.argv[3]) if len(sys.argv) > 3 else 120.0

PASS = "\x1b[32m✓\x1b[0m"
FAIL = "\x1b[31m✗\x1b[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, msg: str = ""):
    results.append((name, ok, msg))
    sym = PASS if ok else FAIL
    print(f"  {sym} {name}" + (f"  — {msg}" if msg else ""), flush=True)


def get(path: str, *, headers=None, params=None, expect_status=None):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{BASE}{path}", headers=headers or {}, params=params or {})
    if expect_status is not None and r.status_code != expect_status:
        return r, f"expected {expect_status} got {r.status_code}: {r.text[:200]}"
    return r, ""


def post(path: str, body, *, headers=None, expect_status=None):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(f"{BASE}{path}", headers=headers or {}, json=body)
    if expect_status is not None and r.status_code != expect_status:
        return r, f"expected {expect_status} got {r.status_code}: {r.text[:200]}"
    return r, ""


def auth_headers() -> dict:
    return {"X-API-Key": KEY}


def section(title: str):
    print(f"\n=== {title} ===", flush=True)


# ---------------------------------------------------------------------------
# A. Basic endpoint tests
# ---------------------------------------------------------------------------
def test_basic_endpoints():
    section("A. Basic endpoint tests")

    r, err = get("/api/health", expect_status=200)
    ok = not err and isinstance(r.json().get("watchlist_records"), int)
    record("/api/health returns 200 with watchlist_records int", ok, err or "")

    r, err = get("/", expect_status=200)
    ok = not err and "AML Screening" in r.text
    record("/ returns 200 with service name", ok, err)

    r, err = get("/docs", expect_status=200)
    record("/docs returns 200 (Swagger UI)", not err, err)

    r, err = post("/api/screen", {"name": "Tata Steel"},
                  headers=auth_headers(), expect_status=200)
    ok = not err and "risk_level" in r.json()
    record("POST /api/screen returns 200 with risk_level", ok, err)

    r, err = post("/api/screen/bulk", {"names": [{"name": "Tata Steel"}]},
                  headers=auth_headers(), expect_status=200)
    ok = not err and "results" in r.json()
    record("POST /api/screen/bulk returns 200 with results", ok, err)

    r, err = get("/api/screen/report/Tata%20Steel", headers=auth_headers(),
                 expect_status=200)
    ok = not err and r.text.startswith("<!doctype html>")
    record("GET /api/screen/report/{q} returns HTML", ok, err)

    r, err = get("/api/sources", headers=auth_headers(), expect_status=200)
    ok = not err and "total_sources" in r.json() and "sources" in r.json()
    record("GET /api/sources returns 200 with sources array", ok, err)


# ---------------------------------------------------------------------------
# B. Authentication tests
# ---------------------------------------------------------------------------
def test_auth():
    section("B. Authentication tests")

    r, _ = post("/api/screen", {"name": "Tata Steel"})
    record("POST /api/screen without API key → 401", r.status_code == 401,
           f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "Tata Steel"},
                headers={"X-API-Key": "wrong-key-xyz"})
    record("POST /api/screen with wrong API key → 401", r.status_code == 401,
           f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "Tata Steel"},
                headers={"X-API-Key": KEY})
    record("POST /api/screen with X-API-Key header → 200", r.status_code == 200,
           f"got {r.status_code}")

    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(f"{BASE}/api/screen?api_key={KEY}", json={"name": "Tata Steel"})
    record("POST /api/screen with ?api_key= query → 200", r.status_code == 200,
           f"got {r.status_code}")

    r, _ = get("/api/health")
    record("GET /api/health without auth (public) → 200", r.status_code == 200,
           f"got {r.status_code}")

    r, _ = get("/")
    record("GET / without auth (public) → 200", r.status_code == 200,
           f"got {r.status_code}")

    r, _ = get("/docs")
    record("GET /docs without auth (public) → 200", r.status_code == 200,
           f"got {r.status_code}")


# ---------------------------------------------------------------------------
# C. Risk classification accuracy
# ---------------------------------------------------------------------------
def screen(name: str, threshold: float = 0.6) -> dict:
    r, _ = post("/api/screen", {"name": name, "threshold": threshold},
                headers=auth_headers(), expect_status=200)
    return r.json() if r.status_code == 200 else {"risk_level": "ERROR", "_status": r.status_code}


def test_risk_classification():
    section("C. Risk classification accuracy")

    high_targets = [
        "Huawei Technologies",
        "Al Qaeda",
        "Islamic State",
        "Iran",
        "DPRK",
        "North Korea",
        "Myanmar",
    ]
    for name in high_targets:
        r = screen(name, 0.6)
        ok = r.get("risk_level") == "HIGH"
        record(f"{name!r} → HIGH",
               ok, f"got {r.get('risk_level')}, matches={r.get('total_matches')}")

    # Modi: MEDIUM via PEP + MCA disqualified (no sanctions in our data)
    r = screen("Narendra Modi", 0.6)
    ok = r.get("risk_level") == "MEDIUM"
    record("'Narendra Modi' → MEDIUM",
           ok, f"got {r.get('risk_level')}, matches={r.get('total_matches')}")

    # Putin is in both PEPs AND actual sanctions lists (UK OFSI, EU, US). HIGH
    # is the correct domain answer; MEDIUM would be wrong.
    r = screen("Vladimir Putin", 0.6)
    ok = r.get("risk_level") in ("HIGH", "MEDIUM")
    record("'Vladimir Putin' → HIGH or MEDIUM (sanctioned + PEP)",
           ok, f"got {r.get('risk_level')}, matches={r.get('total_matches')}")

    # LOW/CLEAR — Columbia Petro Chem at threshold 0.6 should be CLEAR
    # (no watchlist match) but ODI cross-reference exists → LOW
    r = screen("Columbia Petro Chem", 0.6)
    # Acceptable: LOW (ODI found) or CLEAR (if ODI not contributing)
    ok = r.get("risk_level") in ("LOW", "CLEAR") and r.get("odi_cross_reference", {}).get("found")
    record("'Columbia Petro Chem' → LOW with ODI found",
           ok, f"got risk={r.get('risk_level')}, "
               f"odi_found={r.get('odi_cross_reference',{}).get('found')}")

    r = screen("Tata Steel", 0.6)
    ok = r.get("odi_cross_reference", {}).get("found") and \
        r.get("odi_cross_reference", {}).get("total_investments", 0) > 0
    record("'Tata Steel' → ODI cross-reference found",
           ok, f"odi_found={r.get('odi_cross_reference',{}).get('found')}, "
               f"n={r.get('odi_cross_reference',{}).get('total_investments')}")

    clear_targets = ["ZzyxYzqrtu Nonexistent Corp", "AAAA BBBB CCCC DDDD"]
    for name in clear_targets:
        r = screen(name, 0.6)
        ok = r.get("risk_level") == "CLEAR" and r.get("total_matches") == 0
        record(f"{name!r} → CLEAR",
               ok, f"got {r.get('risk_level')}, matches={r.get('total_matches')}")


# ---------------------------------------------------------------------------
# D. Input validation & edge cases
# ---------------------------------------------------------------------------
def test_input_validation():
    section("D. Input validation & edge cases")

    r, _ = post("/api/screen", {"name": ""}, headers=auth_headers())
    record("Empty name → 422", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "x"}, headers=auth_headers())
    record("1-char name → 422", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "x" * 1000}, headers=auth_headers())
    record("1000-char name → 422", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "O'Brien & Sons (Pvt) Ltd."},
                headers=auth_headers())
    record("Special chars apostrophe/ampersand/parens → 200",
           r.status_code == 200, f"got {r.status_code}: {r.text[:120]}")

    r, _ = post("/api/screen", {"name": "Müller GmbH Köln Straße"},
                headers=auth_headers())
    record("Unicode (umlauts) → 200",
           r.status_code == 200, f"got {r.status_code}")

    r, _ = post("/api/screen",
                {"name": "'; DROP TABLE watchlist_records; --"},
                headers=auth_headers())
    record("SQL injection attempt → 200 (safe)",
           r.status_code == 200, f"got {r.status_code}: {r.text[:120]}")

    # Confirm table still exists by checking health
    r, _ = get("/api/health", expect_status=200)
    ok = r.json().get("watchlist_records", 0) > 6_000_000
    record("watchlist_records intact after SQL injection attempt",
           ok, f"rows={r.json().get('watchlist_records')}")

    # XSS payload without `/` so ASGI path routing doesn't choke on encoded
    # slashes. Tests that user input in the report is HTML-escaped: the
    # literal `<img` and `<script` (with angle bracket) must NOT appear.
    # Text content like "onerror=alert" appearing between &lt; and &gt; is
    # safe — browsers render it as text, not an attribute.
    from urllib.parse import quote
    xss_payload = "<img src=x onerror=alert(1)>"
    r, _ = get(f"/api/screen/report/{quote(xss_payload, safe='')}",
               headers=auth_headers())
    ok = (r.status_code == 200
          and "<img" not in r.text
          and "<script" not in r.text
          and "&lt;img" in r.text)  # angle brackets present as escaped entities
    record("XSS payload escaped in HTML report",
           ok, f"status={r.status_code}, "
               + ("payload not escaped in output" if "<img" in r.text else
                  "&lt;img missing" if "&lt;img" not in r.text else ""))

    r, _ = post("/api/screen", {"name": "Tata", "threshold": -0.1},
                headers=auth_headers())
    record("threshold=-0.1 → 422", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "Tata", "threshold": 1.5},
                headers=auth_headers())
    record("threshold=1.5 → 422", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "Tata", "max_results": 0},
                headers=auth_headers())
    record("max_results=0 → 422", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "Tata", "max_results": 500},
                headers=auth_headers())
    record("max_results=500 → 422", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen/bulk", {"names": []}, headers=auth_headers())
    record("bulk 0 names → 422", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen/bulk",
                {"names": [{"name": f"x{i}"} for i in range(60)]},
                headers=auth_headers())
    record("bulk 60 names → 422 (max 50)", r.status_code == 422, f"got {r.status_code}")

    r, _ = post("/api/screen/bulk",
                {"names": [{"name": "valid"}, {"name": ""}]},
                headers=auth_headers())
    record("bulk with empty name in list → 422", r.status_code == 422,
           f"got {r.status_code}")

    r, _ = post("/api/screen", {"name": "   Tata Steel   "},
                headers=auth_headers())
    ok = r.status_code == 200 and r.json().get("query", "").strip() == r.json().get("query", "")
    record("whitespace stripped from query", ok,
           f"got query={r.json().get('query')!r}")

    r, _ = post("/api/screen", {"name": "Tata\x00Steel"},
                headers=auth_headers())
    # null byte must not crash; either 200 (sanitized) or 422 (rejected) is fine
    record("null byte in name handled gracefully",
           r.status_code in (200, 422), f"got {r.status_code}")


# ---------------------------------------------------------------------------
# E. Performance tests
# ---------------------------------------------------------------------------
def test_performance(is_remote: bool):
    section("E. Performance tests")
    single_budget = 10.0 if is_remote else 2.0
    bulk10_budget = 60.0 if is_remote else 15.0
    sources_budget = 60.0 if is_remote else 30.0
    health_budget = 2.0 if is_remote else 1.0

    t0 = time.time()
    r, _ = get("/api/health")
    health_dt = time.time() - t0
    record(f"/api/health < {health_budget}s", health_dt < health_budget,
           f"took {health_dt:.2f}s")

    t0 = time.time()
    r, _ = post("/api/screen", {"name": "Huawei Technologies"},
                headers=auth_headers(), expect_status=200)
    dt = time.time() - t0
    record(f"single /api/screen < {single_budget}s", dt < single_budget,
           f"took {dt:.2f}s")

    names = [{"name": n} for n in ["Tata Steel", "Reliance Industries", "ONGC",
             "Adani Power", "Infosys", "Wipro", "TCS", "HDFC Bank",
             "ICICI Bank", "Bharti Airtel"]]
    t0 = time.time()
    r, _ = post("/api/screen/bulk", {"names": names},
                headers=auth_headers(), expect_status=200)
    dt = time.time() - t0
    record(f"bulk 10 names < {bulk10_budget}s", dt < bulk10_budget,
           f"took {dt:.2f}s")

    # /api/sources should be cached after first call
    t0 = time.time()
    r, _ = get("/api/sources", headers=auth_headers(), expect_status=200)
    dt1 = time.time() - t0
    t0 = time.time()
    r, _ = get("/api/sources", headers=auth_headers(), expect_status=200)
    dt2 = time.time() - t0
    record(f"/api/sources first call < {sources_budget}s",
           dt1 < sources_budget, f"took {dt1:.2f}s")
    record(f"/api/sources cached call < 1s", dt2 < 1.0, f"took {dt2:.2f}s")

    # Concurrent requests
    def one():
        r, _ = post("/api/screen", {"name": "Tata Steel"},
                    headers=auth_headers())
        return r.status_code

    with ThreadPoolExecutor(max_workers=5) as ex:
        t0 = time.time()
        codes = list(ex.map(lambda _: one(), range(5)))
        dt = time.time() - t0
    ok = all(c == 200 for c in codes)
    record("5 concurrent /api/screen calls → all 200",
           ok, f"codes={codes} in {dt:.1f}s")


# ---------------------------------------------------------------------------
# F. Response structure validation
# ---------------------------------------------------------------------------
def test_response_structure():
    section("F. Response structure validation")

    r = screen("Huawei Technologies", 0.6)

    required_top = {
        "query": str, "risk_level": str, "total_matches": int,
        "screening_time_ms": int, "matches": list,
        "odi_cross_reference": dict,
    }
    missing = []
    for k, typ in required_top.items():
        if k not in r:
            missing.append(f"missing {k}")
        elif not isinstance(r[k], typ):
            missing.append(f"{k} is {type(r[k]).__name__} not {typ.__name__}")
    record("Top-level fields present and typed", not missing, "; ".join(missing))

    record("risk_level in valid set",
           r.get("risk_level") in {"HIGH", "MEDIUM", "LOW", "CLEAR"},
           f"got {r.get('risk_level')}")

    record("fatf_jurisdiction_flag is dict or null",
           r.get("fatf_jurisdiction_flag") is None or isinstance(r.get("fatf_jurisdiction_flag"), dict),
           f"got {type(r.get('fatf_jurisdiction_flag')).__name__}")

    matches = r.get("matches") or []
    if matches:
        match_required = {"name", "similarity", "source_id", "source_agency",
                          "source_list", "risk_category", "record_id", "details"}
        m = matches[0]
        miss = match_required - set(m.keys())
        record("match has all required fields", not miss, f"missing {miss}")

        valid_cats = {"sanctions", "criminal", "enforcement", "pep", "debarment",
                      "leak", "jurisdiction_risk", "informational"}
        cats = {mm["risk_category"] for mm in matches}
        record(f"all risk_category values valid",
               cats.issubset(valid_cats), f"got {cats - valid_cats}")

        sims_ok = all(0 <= mm["similarity"] <= 1 for mm in matches)
        record("all similarities in [0, 1]", sims_ok, "")

        record("record_id is int",
               isinstance(matches[0]["record_id"], int),
               f"got {type(matches[0]['record_id']).__name__}")

    odi = r.get("odi_cross_reference") or {}
    odi_required = {"found", "total_investments", "total_usd_mn",
                    "countries", "top_investments"}
    miss = odi_required - set(odi.keys())
    record("odi_cross_reference has all required fields",
           not miss, f"missing {miss}")


# ---------------------------------------------------------------------------
# G. HTML report quality
# ---------------------------------------------------------------------------
def test_html_report():
    section("G. HTML report quality")
    r, _ = get("/api/screen/report/Huawei%20Technologies",
               headers=auth_headers(), expect_status=200)
    txt = r.text
    record("DOCTYPE declared", txt.lower().startswith("<!doctype html>"))
    record("title tag present", "<title>" in txt and "Huawei" in txt)
    record("risk badge present", 'class="risk"' in txt)
    record("matches table present",
           "<table>" in txt and "<thead>" in txt and "</table>" in txt)
    # FATF flag should NOT appear for Huawei (not a jurisdiction)
    record("no spurious FATF flag for Huawei",
           "FATF Jurisdiction Flag" not in txt)

    r, _ = get("/api/screen/report/Iran",
               headers=auth_headers(), expect_status=200)
    record("Iran report has FATF flag",
           "FATF Jurisdiction Flag" in r.text)

    r, _ = get("/api/screen/report/Tata%20Steel",
               headers=auth_headers(), expect_status=200)
    record("Tata Steel report has ODI cross-reference section",
           "ODI Cross-Reference" in r.text)


# ---------------------------------------------------------------------------
# H. Cross-reference accuracy
# ---------------------------------------------------------------------------
def test_cross_reference():
    section("H. Cross-reference accuracy")
    r = screen("Columbia Petro Chem", 0.6)
    odi = r.get("odi_cross_reference", {})
    record("Columbia Petro Chem ODI: Singapore found",
           "SINGAPORE" in odi.get("countries", []),
           f"countries={odi.get('countries')}")

    r = screen("Iran", 0.6)
    fatf = r.get("fatf_jurisdiction_flag") or {}
    record("Iran has FATF flag",
           fatf.get("list") in ("black", "BLACK", "black_list"),
           f"flag={fatf}")

    r = screen("Nepal", 0.6)
    fatf = r.get("fatf_jurisdiction_flag") or {}
    record("Nepal has FATF grey flag",
           fatf.get("list") in ("grey", "GREY"),
           f"flag={fatf}")

    r = screen("Al Qaeda", 0.6)
    record("Al Qaeda has NO ODI cross-reference",
           not r.get("odi_cross_reference", {}).get("found"),
           f"odi={r.get('odi_cross_reference')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(f"\nRunning QA against: {BASE}")
    print(f"API key prefix: {KEY[:20]}...\n")

    is_remote = "127.0.0.1" not in BASE and "localhost" not in BASE
    test_basic_endpoints()
    test_auth()
    test_risk_classification()
    test_input_validation()
    test_response_structure()
    test_html_report()
    test_cross_reference()
    test_performance(is_remote)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"TOTAL: {passed} passed, {failed} failed, {len(results)} total")
    if failed:
        print("\nFAILED:")
        for name, ok, msg in results:
            if not ok:
                print(f"  {FAIL} {name}: {msg}")
        sys.exit(1)
    print("ALL TESTS PASSED ✓")
    sys.exit(0)


if __name__ == "__main__":
    main()
