"""
AML Screening API.

Fuzzy-matches a name or company against ~6.4M watchlist_records using
trigram similarity (pg_trgm), cross-references ODI overseas investments,
and computes a risk level (HIGH / MEDIUM / LOW / CLEAR) from the sources
that returned matches.

Endpoints:
    POST /api/screen              - single name screening
    POST /api/screen/bulk         - batch screening
    GET  /api/screen/report/{q}   - formatted HTML screening report
    GET  /api/sources             - active sources with row counts
    GET  /api/health              - DB connectivity + row count (public)

DB target is selected by env var SCREEN_DB:
    SCREEN_DB=local (default)
    SCREEN_DB=rds

Run:
    venv/bin/uvicorn api.screening_api:app --host 0.0.0.0 --port 8002 --reload
"""
import html
import os
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Literal, Optional

import psycopg2.extras
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader, APIKeyQuery
from psycopg2.pool import ThreadedConnectionPool
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config

DB_CONFIGS = {
    "local": dict(host="localhost", user="aayush", password="aayush123",
                  dbname="risk_pipeline"),
    "rds":   dict(host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
                  user="aayush", password="Aaayyuusshhh",
                  dbname="risk_pipeline", connect_timeout=10),
}
DB_TARGET = os.environ.get("SCREEN_DB", "local").lower()
if DB_TARGET not in DB_CONFIGS:
    raise RuntimeError(f"SCREEN_DB must be one of {list(DB_CONFIGS)}; got {DB_TARGET!r}")
DB_CONFIG = DB_CONFIGS[DB_TARGET]

API_KEY = os.environ.get("SCREENING_API_KEY", "")

DEFAULT_THRESHOLD = 0.6
DEFAULT_MAX_RESULTS = 20
BULK_MAX_NAMES = 50

# Specific overrides checked before generic substring patterns. These exist
# because some source_ids contain misleading substrings (e.g. opensanctions_peps
# contains "sanctions" but is a PEP list, not a sanctions list).
_CATEGORY_OVERRIDES = (
    ("opensanctions_peps",        "pep"),
    ("opensanctions_debarment",   "debarment"),
    ("opensanctions_crime",       "criminal"),
    ("fatf_blacklist",            "jurisdiction_risk"),
    ("fatf_greylist",             "jurisdiction_risk"),
    ("interpol",                  "criminal"),
    ("mca_proclaimed_offenders",  "criminal"),
    ("icij_",                     "leak"),
)
# Generic substring patterns by category.
_CATEGORY_PATTERNS = (
    ("sanctions", ("_sanctions", "sanctions_", "ofac", "un_sc", "un_consolidated", "_csl_", "us_csl")),
    ("enforcement", ("sebi_", "_sebi", "mca_defaulter", "mca_disqualified", "mca_vanishing",
                     "mca_corporate_fraud", "fiu_", "_fiu", "bse_debarred", "nse_debarred",
                     "ed_enforce", "nia_wanted", "cbi_wanted")),
    ("debarment", ("adb_", "afdb_", "ebrd_", "idb_", "worldbank_")),
)
# Mapping risk_category -> risk_level (HIGH/MEDIUM/LOW).
_CATEGORY_TO_LEVEL = {
    "sanctions": "HIGH",
    "criminal": "HIGH",
    "pep": "MEDIUM",
    "debarment": "MEDIUM",
    "enforcement": "MEDIUM",
    "leak": "MEDIUM",
    "jurisdiction_risk": "HIGH",   # overridden below for greylist
    "informational": "LOW",
}

# ---------------------------------------------------------------------------
# App

app = FastAPI(
    title="AML Screening API",
    description=(
        "Screen names and companies against 6.4M+ watchlist records: OFAC, "
        "UN, EU, FATF, Interpol, OpenSanctions, RBI, SEBI, MCA, FIU and more. "
        "Returns matches with similarity scores and a risk level."
    ),
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

POOL: Optional[ThreadedConnectionPool] = None


@app.on_event("startup")
def _startup():
    global POOL
    POOL = ThreadedConnectionPool(1, 10, **DB_CONFIG)


@app.on_event("shutdown")
def _shutdown():
    if POOL is not None:
        POOL.closeall()


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_api_key_query = APIKeyQuery(name="api_key", auto_error=False)


def verify_api_key(
    header_key: Optional[str] = Security(_api_key_header),
    query_key: Optional[str] = Security(_api_key_query),
):
    if not API_KEY:
        raise HTTPException(status_code=503, detail="API key not configured on server")
    presented = header_key or query_key
    if not presented or presented != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return presented


@contextmanager
def cursor(dict_rows: bool = True):
    conn = POOL.getconn()
    try:
        cur = conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None
        )
        try:
            yield cur
        finally:
            cur.close()
    finally:
        POOL.putconn(conn)


def _to_json(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _row(r):
    return {k: _to_json(v) for k, v in dict(r).items()}


# ---------------------------------------------------------------------------
# Risk classification

def _risk_category(source_id: str) -> str:
    sid = (source_id or "").lower()
    for needle, cat in _CATEGORY_OVERRIDES:
        if needle in sid:
            return cat
    for cat, patterns in _CATEGORY_PATTERNS:
        if any(p in sid for p in patterns):
            return cat
    return "informational"


def _level_for(source_id: str) -> str:
    sid = (source_id or "").lower()
    cat = _risk_category(sid)
    # FATF greylist downgrades to MEDIUM, blacklist stays HIGH.
    if cat == "jurisdiction_risk" and "fatf_greylist" in sid:
        return "MEDIUM"
    return _CATEGORY_TO_LEVEL.get(cat, "LOW")


def _risk_level(matches: list[dict], odi_found: bool = False) -> str:
    levels = {_level_for(m.get("source_id") or "") for m in matches}
    if "HIGH" in levels:
        return "HIGH"
    if "MEDIUM" in levels:
        return "MEDIUM"
    if matches or odi_found:
        return "LOW"
    return "CLEAR"


# ---------------------------------------------------------------------------
# Search

WATCHLIST_COLS = (
    "id, source_id, source_agency, source_list, name, father_name, "
    "date_of_birth, address, details, detail_page_url, link_kind"
)


def _search_watchlist(cur, query: str, threshold: float, max_results: int) -> list[dict]:
    """Tier 1: exact lower-case match. Tier 2: trigram fuzzy via % operator.
    Combined and deduped by id, ordered by similarity desc."""
    cur.execute("SET LOCAL pg_trgm.similarity_threshold = %s;", (threshold,))

    # Exact match - similarity forced to 1.0 so it sorts first.
    cur.execute(
        f"""
        SELECT {WATCHLIST_COLS}, 1.0::real AS similarity
        FROM watchlist_records
        WHERE lower(name) = lower(%s)
        LIMIT %s;
        """,
        (query, max_results),
    )
    exact = [_row(r) for r in cur.fetchall()]
    exact_ids = {r["id"] for r in exact}
    remaining = max(0, max_results - len(exact))

    fuzzy: list[dict] = []
    if remaining > 0:
        cur.execute(
            f"""
            SELECT {WATCHLIST_COLS}, similarity(name, %s) AS similarity
            FROM watchlist_records
            WHERE name %% %s
              AND similarity(name, %s) >= %s
              AND lower(name) != lower(%s)
            ORDER BY similarity DESC, length(name) ASC
            LIMIT %s;
            """,
            (query, query, query, threshold, query, remaining),
        )
        fuzzy = [_row(r) for r in cur.fetchall()]

    out = exact + [r for r in fuzzy if r["id"] not in exact_ids]
    for r in out:
        r["similarity"] = round(float(r["similarity"]), 4)
        r["risk_category"] = _risk_category(r["source_id"])
        r["record_id"] = r.pop("id")
    return out


def _odi_cross_reference(cur, query: str) -> dict:
    cur.execute(
        """
        SELECT indian_party, jv_wos_name, country, total_usd_mn, period_from
        FROM rbi_odi_investments
        WHERE indian_party ILIKE %s
        ORDER BY total_usd_mn DESC NULLS LAST
        LIMIT 50;
        """,
        (f"%{query}%",),
    )
    rows = [_row(r) for r in cur.fetchall()]
    if not rows:
        return {"found": False, "total_investments": 0, "total_usd_mn": 0.0,
                "countries": [], "top_investments": []}
    total = sum(float(r["total_usd_mn"] or 0) for r in rows)
    countries = sorted({r["country"] for r in rows if r.get("country")})
    return {
        "found": True,
        "total_investments": len(rows),
        "total_usd_mn": round(total, 4),
        "countries": countries,
        "top_investments": rows[:5],
    }


def _fatf_jurisdiction_flag(matches: list[dict]) -> Optional[dict]:
    """If any match is FATF black/grey, return a structured flag."""
    for m in matches:
        sid = (m.get("source_id") or "").lower()
        if sid == "fatf_blacklist":
            return {"list": "black", "name": m.get("name"),
                    "details": m.get("details")}
        if sid == "fatf_greylist":
            return {"list": "grey", "name": m.get("name"),
                    "details": m.get("details")}
    return None


def _screen_one(cur, name: str, threshold: float, max_results: int) -> dict:
    t0 = time.perf_counter()
    matches = _search_watchlist(cur, name, threshold, max_results)
    odi = _odi_cross_reference(cur, name)
    fatf = _fatf_jurisdiction_flag(matches)
    risk = _risk_level(matches, odi_found=odi["found"])
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "query": name,
        "risk_level": risk,
        "total_matches": len(matches),
        "screening_time_ms": elapsed_ms,
        "matches": matches,
        "odi_cross_reference": odi,
        "fatf_jurisdiction_flag": fatf,
    }


# ---------------------------------------------------------------------------
# Pydantic models

class ScreenRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=500)
    type: Literal["company", "person", "auto"] = "auto"
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0.1, le=1.0)
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=200)


class BulkScreenItem(BaseModel):
    name: str = Field(..., min_length=2, max_length=500)
    type: Literal["company", "person", "auto"] = "auto"


class BulkScreenRequest(BaseModel):
    names: list[BulkScreenItem] = Field(..., min_length=1, max_length=BULK_MAX_NAMES)
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0.1, le=1.0)
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=200)


# ---------------------------------------------------------------------------
# Endpoints

@app.get("/")
def root():
    return {
        "service": "AML Screening API",
        "version": "1.0.0",
        "db_target": DB_TARGET,
        "endpoints": [
            "/api/health",
            "/api/screen          (POST)",
            "/api/screen/bulk     (POST)",
            "/api/screen/report/{query}",
            "/api/sources",
            "/docs",
        ],
    }


@app.get("/api/health")
def health():
    try:
        with cursor() as cur:
            cur.execute("SELECT COUNT(*) AS rows FROM watchlist_records;")
            wl = cur.fetchone()["rows"]
            cur.execute("SELECT COUNT(*) AS rows FROM rbi_odi_investments;")
            odi = cur.fetchone()["rows"]
        return {
            "status": "ok",
            "db_target": DB_TARGET,
            "watchlist_records": wl,
            "rbi_odi_investments": odi,
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unavailable: {e}")


@app.post("/api/screen", dependencies=[Depends(verify_api_key)])
def screen(req: ScreenRequest):
    with cursor() as cur:
        return _screen_one(cur, req.name, req.threshold, req.max_results)


@app.post("/api/screen/bulk", dependencies=[Depends(verify_api_key)])
def screen_bulk(req: BulkScreenRequest):
    t0 = time.perf_counter()
    results = []
    with cursor() as cur:
        for item in req.names:
            results.append(_screen_one(cur, item.name, req.threshold, req.max_results))
    return {
        "results": results,
        "total_screened": len(results),
        "total_time_ms": int((time.perf_counter() - t0) * 1000),
    }


@app.get("/api/screen/report/{query}", response_class=HTMLResponse,
         dependencies=[Depends(verify_api_key)])
def screen_report(
    query: str = Path(..., min_length=2, max_length=500),
    threshold: float = Query(DEFAULT_THRESHOLD, ge=0.1, le=1.0),
    max_results: int = Query(DEFAULT_MAX_RESULTS, ge=1, le=200),
):
    with cursor() as cur:
        result = _screen_one(cur, query, threshold, max_results)
    return HTMLResponse(_render_html_report(result))


@app.get("/api/sources", dependencies=[Depends(verify_api_key)])
def sources(
    min_count: int = Query(1, ge=0, description="Only sources with at least N records"),
):
    with cursor() as cur:
        cur.execute(
            """
            SELECT source_id,
                   MAX(source_agency) AS agency,
                   MAX(source_list) AS list_name,
                   COUNT(*) AS count
            FROM watchlist_records
            WHERE source_id IS NOT NULL AND source_id != ''
            GROUP BY source_id
            HAVING COUNT(*) >= %s
            ORDER BY count DESC;
            """,
            (min_count,),
        )
        rows = [_row(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS total FROM watchlist_records;")
        total = cur.fetchone()["total"]
    return {
        "total_sources": len(rows),
        "total_records": total,
        "sources": rows,
    }


# ---------------------------------------------------------------------------
# HTML report

RISK_COLORS = {
    "HIGH": "#c62828",
    "MEDIUM": "#ef6c00",
    "LOW": "#2e7d32",
    "CLEAR": "#1565c0",
}


def _render_html_report(r: dict) -> str:
    color = RISK_COLORS.get(r["risk_level"], "#555")
    q = html.escape(r["query"])
    rows_html = []
    for m in r["matches"]:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(m.get('name') or '')}</td>"
            f"<td>{m['similarity']:.2f}</td>"
            f"<td>{html.escape((m.get('source_agency') or '') + ' / ' + (m.get('source_list') or ''))}</td>"
            f"<td>{html.escape(m.get('risk_category') or '')}</td>"
            f"<td>{html.escape((m.get('details') or '')[:200])}</td>"
            "</tr>"
        )
    matches_table = (
        "<table><thead><tr><th>Name</th><th>Sim</th><th>Source</th>"
        "<th>Category</th><th>Details</th></tr></thead><tbody>"
        + "".join(rows_html) + "</tbody></table>"
    ) if r["matches"] else "<p><em>No matches above threshold.</em></p>"

    odi = r["odi_cross_reference"]
    odi_html = ""
    if odi["found"]:
        odi_rows = "".join(
            f"<li>{html.escape(t.get('indian_party') or '')} → "
            f"{html.escape(t.get('jv_wos_name') or '')} "
            f"({html.escape(t.get('country') or '')}) "
            f"USD {float(t.get('total_usd_mn') or 0):.2f}mn"
            f" ({html.escape(t.get('period_from') or '')})</li>"
            for t in odi["top_investments"]
        )
        odi_html = (
            f"<h3>ODI Cross-Reference</h3>"
            f"<p>{odi['total_investments']} overseas investments, "
            f"total USD {odi['total_usd_mn']:.2f}mn, countries: "
            f"{html.escape(', '.join(odi['countries']))}.</p>"
            f"<ul>{odi_rows}</ul>"
        )

    fatf = r.get("fatf_jurisdiction_flag")
    fatf_html = ""
    if fatf:
        fatf_html = (
            f"<h3>FATF Jurisdiction Flag</h3>"
            f"<p><strong>{fatf['list'].upper()} LIST</strong>: "
            f"{html.escape(fatf.get('name') or '')}</p>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Screening Report: {q}</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ margin-bottom: 0; }}
.risk {{ display: inline-block; padding: 0.4em 1em; color: white; font-weight: bold;
        border-radius: 4px; background: {color}; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1em; font-size: 0.9em; }}
th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #ddd; vertical-align: top; }}
th {{ background: #f5f5f5; }}
tr:hover {{ background: #fafafa; }}
.meta {{ color: #666; font-size: 0.9em; margin-top: 0; }}
</style></head><body>
<h1>Screening Report</h1>
<p class="meta">Query: <strong>{q}</strong> &mdash; {r['total_matches']} matches in {r['screening_time_ms']}ms</p>
<p class="risk">RISK: {r['risk_level']}</p>
{fatf_html}
<h3>Watchlist Matches</h3>
{matches_table}
{odi_html}
</body></html>"""
