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
    GET  /api/sources             - active sources with row counts (cached 5min)
    GET  /api/health              - DB connectivity + row count (public)

DB target is selected by env var SCREEN_DB:
    SCREEN_DB=local (default)
    SCREEN_DB=rds

Run:
    venv/bin/uvicorn api.screening_api:app --host 0.0.0.0 --port 8002 --reload
"""
import html
import logging
import logging.handlers
import os
import threading
import time
import unicodedata
from contextlib import contextmanager
from decimal import Decimal
from typing import Literal, Optional

import psycopg2
import psycopg2.extras
from fastapi import (Depends, FastAPI, HTTPException, Path, Query, Request,
                     Security)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader, APIKeyQuery
from psycopg2.pool import ThreadedConnectionPool
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Config

DB_CONFIGS = {
    "local": dict(host="localhost", user="aayush", password="aayush123",
                  dbname="risk_pipeline"),
    "rds":   dict(host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
                  user="aayush", password="Aaayyuusshhh",
                  dbname="risk_pipeline", connect_timeout=10,
                  keepalives=1, keepalives_idle=30,
                  keepalives_interval=10, keepalives_count=5),
}
DB_TARGET = os.environ.get("SCREEN_DB", "local").lower()
if DB_TARGET not in DB_CONFIGS:
    raise RuntimeError(f"SCREEN_DB must be one of {list(DB_CONFIGS)}; got {DB_TARGET!r}")
DB_CONFIG = DB_CONFIGS[DB_TARGET]

API_KEY = os.environ.get("SCREENING_API_KEY", "")

DEFAULT_THRESHOLD = 0.6
DEFAULT_MAX_RESULTS = 20
BULK_MAX_NAMES = 50
SOURCES_CACHE_TTL = 300  # 5 minutes
DB_POOL_MIN = int(os.environ.get("SCREEN_POOL_MIN", "2"))
DB_POOL_MAX = int(os.environ.get("SCREEN_POOL_MAX", "15"))
DB_RETRY_ON_TRANSIENT = True

# ---------------------------------------------------------------------------
# Risk classification tables

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
_CATEGORY_PATTERNS = (
    ("sanctions", ("_sanctions", "sanctions_", "ofac", "un_sc", "un_consolidated",
                   "_csl_", "us_csl", "consolidated_screening")),
    ("enforcement", ("sebi_", "_sebi", "mca_defaulter", "mca_disqualified",
                     "mca_vanishing", "mca_corporate_fraud", "fiu_", "_fiu",
                     "bse_debarred", "nse_debarred", "ed_enforce",
                     "nia_wanted", "cbi_wanted")),
    ("debarment", ("adb_", "afdb_", "ebrd_", "idb_", "worldbank_")),
)
_CATEGORY_TO_LEVEL = {
    "sanctions": "HIGH",
    "criminal": "HIGH",
    "pep": "MEDIUM",
    "debarment": "MEDIUM",
    "enforcement": "MEDIUM",
    "leak": "MEDIUM",
    "jurisdiction_risk": "HIGH",   # overridden for fatf_greylist below
    "informational": "LOW",
}

# ---------------------------------------------------------------------------
# Logging

_log_dir = os.environ.get("SCREEN_LOG_DIR", "logs")
os.makedirs(_log_dir, exist_ok=True)
_logger = logging.getLogger("screening_api")
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    _formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "screening_api.log"),
        maxBytes=10_000_000, backupCount=5, encoding="utf-8",
    )
    _file_handler.setFormatter(_formatter)
    _logger.addHandler(_file_handler)
    _stderr = logging.StreamHandler()
    _stderr.setFormatter(_formatter)
    _logger.addHandler(_stderr)


def _log_event(kind: str, ip: str, **fields):
    parts = [f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}"
             for k, v in fields.items()]
    _logger.info(f"{kind} ip={ip} {' '.join(parts)}")


# ---------------------------------------------------------------------------
# App

app = FastAPI(
    title="AML Screening API",
    description=(
        "Screen names and companies against 6.4M+ watchlist records: OFAC, "
        "UN, EU, FATF, Interpol, OpenSanctions, RBI, SEBI, MCA, FIU and more. "
        "Returns matches with similarity scores and a risk level."
    ),
    version="1.1.0",
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
    POOL = ThreadedConnectionPool(DB_POOL_MIN, DB_POOL_MAX, **DB_CONFIG)
    _logger.info(f"startup db_target={DB_TARGET} pool={DB_POOL_MIN}-{DB_POOL_MAX}")


@app.on_event("shutdown")
def _shutdown():
    if POOL is not None:
        POOL.closeall()


# ---------------------------------------------------------------------------
# Error handlers — never leak a raw traceback to clients

def _serialize_validation_errors(errors: list) -> list:
    """Pydantic v2 errors include a raw exception in ctx['error'] which isn't
    JSON-serializable. Stringify it."""
    clean = []
    for e in errors:
        ce = {k: v for k, v in e.items() if k != "ctx"}
        ctx = e.get("ctx")
        if ctx:
            ce["ctx"] = {k: (str(v) if isinstance(v, Exception) else v)
                         for k, v in ctx.items()}
        clean.append(ce)
    return clean


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error",
                 "detail": _serialize_validation_errors(exc.errors()),
                 "status_code": 422},
    )


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "http_error", "detail": exc.detail,
                 "status_code": exc.status_code},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    _logger.exception(f"unhandled error on {request.method} {request.url.path}")
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error",
                 "detail": "An unexpected error occurred. Check server logs.",
                 "status_code": 500},
    )


# ---------------------------------------------------------------------------
# Auth

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


# ---------------------------------------------------------------------------
# DB

@contextmanager
def cursor(dict_rows: bool = True):
    """Yield a cursor from the pool. On the FIRST attempt, retry once on a
    transient psycopg2.OperationalError (e.g. RDS dropped the connection)."""
    attempts = 0
    last_err = None
    while attempts < (2 if DB_RETRY_ON_TRANSIENT else 1):
        attempts += 1
        try:
            conn = POOL.getconn()
        except Exception as e:
            last_err = e
            time.sleep(0.2)
            continue
        try:
            # Test the connection is alive on retry attempt
            if attempts > 1:
                try:
                    test_cur = conn.cursor()
                    test_cur.execute("SELECT 1;")
                    test_cur.fetchone()
                    test_cur.close()
                except Exception:
                    POOL.putconn(conn, close=True)
                    continue
            cur = conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None
            )
            try:
                yield cur
            finally:
                cur.close()
            POOL.putconn(conn)
            return
        except psycopg2.OperationalError as e:
            last_err = e
            try:
                POOL.putconn(conn, close=True)
            except Exception:
                pass
            _logger.warning(f"transient DB error on attempt {attempts}: {e}")
            time.sleep(0.3)
        except Exception:
            POOL.putconn(conn)
            raise
    raise HTTPException(status_code=503,
                        detail=f"DB unavailable after {attempts} attempts: {last_err}")


def _to_json(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _row(r):
    return {k: _to_json(v) for k, v in dict(r).items()}


# ---------------------------------------------------------------------------
# Input sanitization

def _sanitize_name(s: str) -> str:
    """Strip surrounding whitespace, normalize unicode (NFC), remove null bytes."""
    if s is None:
        return ""
    # NFC normalize so Müller and decomposed forms match the same way
    s = unicodedata.normalize("NFC", s)
    # Remove null bytes — psycopg2 rejects them and they have no legitimate use
    s = s.replace("\x00", "")
    # Strip control chars except common whitespace (tab, newline, cr)
    s = "".join(c for c in s if c == "\t" or c == "\n" or c == "\r"
                or ord(c) >= 0x20)
    return s.strip()


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
    """Three-tier search:
    1. Exact lower-case match (similarity forced to 1.0)
    2. Case-insensitive substring (ILIKE %q% — uses GIN trgm index). Catches
       short queries like "DPRK" matching "DPRK (North Korea)".
    3. Trigram fuzzy via the % operator with the user's threshold.
    Combined, deduped by id, ordered by similarity desc."""
    cur.execute("SET LOCAL pg_trgm.similarity_threshold = %s;", (threshold,))

    # Tier 1: exact
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
    seen_ids = {r["id"] for r in exact}
    remaining = max_results - len(exact)

    # Tier 2: substring ILIKE — only meaningful for queries >= 3 chars (GIN
    # trgm extracts 3-char trigrams). Excludes ids already in exact.
    substring: list[dict] = []
    if remaining > 0 and len(query) >= 3:
        cur.execute(
            f"""
            SELECT {WATCHLIST_COLS}, similarity(name, %s) AS similarity
            FROM watchlist_records
            WHERE name ILIKE %s
              AND lower(name) != lower(%s)
              AND ({"NOT (id = ANY(%s))" if seen_ids else "TRUE"})
            ORDER BY similarity(name, %s) DESC, length(name) ASC
            LIMIT %s;
            """ if seen_ids else
            f"""
            SELECT {WATCHLIST_COLS}, similarity(name, %s) AS similarity
            FROM watchlist_records
            WHERE name ILIKE %s
              AND lower(name) != lower(%s)
            ORDER BY similarity(name, %s) DESC, length(name) ASC
            LIMIT %s;
            """,
            ((query, f"%{query}%", query, list(seen_ids), query, remaining)
             if seen_ids else
             (query, f"%{query}%", query, query, remaining)),
        )
        substring = [_row(r) for r in cur.fetchall()]
        seen_ids.update(r["id"] for r in substring)
        remaining = max_results - len(exact) - len(substring)

    # Tier 3: trigram fuzzy
    fuzzy: list[dict] = []
    if remaining > 0:
        cur.execute(
            f"""
            SELECT {WATCHLIST_COLS}, similarity(name, %s) AS similarity
            FROM watchlist_records
            WHERE name %% %s
              AND similarity(name, %s) >= %s
              AND ({"NOT (id = ANY(%s))" if seen_ids else "TRUE"})
            ORDER BY similarity DESC, length(name) ASC
            LIMIT %s;
            """ if seen_ids else
            f"""
            SELECT {WATCHLIST_COLS}, similarity(name, %s) AS similarity
            FROM watchlist_records
            WHERE name %% %s
              AND similarity(name, %s) >= %s
            ORDER BY similarity DESC, length(name) ASC
            LIMIT %s;
            """,
            ((query, query, query, threshold, list(seen_ids), remaining)
             if seen_ids else
             (query, query, query, threshold, remaining)),
        )
        fuzzy = [_row(r) for r in cur.fetchall()]

    out = exact + substring + fuzzy
    # Sort the combined set by similarity desc so substring matches with
    # high computed similarity outrank trigram matches with lower scores.
    out.sort(key=lambda r: (-float(r["similarity"]), len(r.get("name") or "")))

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
    name = _sanitize_name(name)
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
# Pydantic models with sanitization

def _validate_name(v: str) -> str:
    v = _sanitize_name(v)
    if len(v) < 2:
        raise ValueError("name must be at least 2 characters after sanitization")
    if len(v) > 500:
        raise ValueError("name must be at most 500 characters")
    return v


class ScreenRequest(BaseModel):
    name: str
    type: Literal["company", "person", "auto"] = "auto"
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0.1, le=1.0)
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=200)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _validate_name(v)


class BulkScreenItem(BaseModel):
    name: str
    type: Literal["company", "person", "auto"] = "auto"

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _validate_name(v)


class BulkScreenRequest(BaseModel):
    names: list[BulkScreenItem] = Field(..., min_length=1, max_length=BULK_MAX_NAMES)
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0.1, le=1.0)
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=200)


# ---------------------------------------------------------------------------
# /api/sources cache

_sources_cache_lock = threading.Lock()
_sources_cache: dict = {"key": None, "data": None, "ts": 0.0}


def _sources_cached(cur, min_count: int) -> dict:
    now = time.time()
    key = ("sources", min_count)
    with _sources_cache_lock:
        if (_sources_cache["key"] == key and
                (now - _sources_cache["ts"]) < SOURCES_CACHE_TTL and
                _sources_cache["data"] is not None):
            return _sources_cache["data"]
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
    result = {
        "total_sources": len(rows),
        "total_records": total,
        "sources": rows,
        "cached_at": int(now),
        "cache_ttl_seconds": SOURCES_CACHE_TTL,
    }
    with _sources_cache_lock:
        _sources_cache["key"] = key
        _sources_cache["data"] = result
        _sources_cache["ts"] = now
    return result


# ---------------------------------------------------------------------------
# Endpoints

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "-"


@app.get("/")
def root():
    return {
        "service": "AML Screening API",
        "version": "1.1.0",
        "db_target": DB_TARGET,
        "endpoints": [
            "/api/health",
            "/api/pipeline/status",
            "/api/screen          (POST)",
            "/api/screen/bulk     (POST)",
            "/api/screen/report/{query}",
            "/api/sources",
            "/docs",
        ],
    }


# ---------------------------------------------------------------------------
# Pipeline status — read-only view of the last daily scrape run.
#
# Reads logs/post_scrape_diff.json (written by scripts/compare_counts.py at
# the end of run_all.sh) and combines it with live DB totals so monitoring
# dashboards can show "we ran today, here's what changed".
#
# Public (no API key) — same auth posture as /api/health.

import json as _pipeline_json  # noqa: E402  local alias to avoid shadowing


def _pipeline_diff_path() -> str:
    """logs/post_scrape_diff.json relative to the repo root. Repo root is
    one level above the api/ dir."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "post_scrape_diff.json",
    )


@app.get("/api/pipeline/status")
def pipeline_status():
    """Surface the most recent daily-pipeline diff + DB row count.

    Returns 200 even if the diff file is missing (e.g. EC2 host that hasn't
    run the daily cron yet) — the response includes a `diff_available` flag
    so clients can render a sensible "no run yet" state.
    """
    path = _pipeline_diff_path()
    diff: dict = {}
    diff_available = False
    if os.path.exists(path):
        try:
            with open(path) as f:
                diff = _pipeline_json.load(f)
                diff_available = True
        except Exception as e:
            return JSONResponse(
                status_code=200,
                content={"diff_available": False,
                         "diff_read_error": f"{type(e).__name__}: {e}"},
            )

    # Live counts. Use the planner estimate for watchlist_records (instant)
    # and read distinct source_ids from source_health (small table — fast)
    # instead of doing COUNT(DISTINCT source_id) over 6M+ rows which is too
    # slow on t4g.micro RDS even with the source_id index.
    db_records = 0
    db_sources = 0
    try:
        with cursor() as cur:
            cur.execute(
                "SELECT n_live_tup FROM pg_stat_user_tables "
                "WHERE relname='watchlist_records';"
            )
            row = cur.fetchone()
            if row:
                db_records = int(row["n_live_tup"])
            cur.execute(
                "SELECT COUNT(DISTINCT source_id) AS n "
                "FROM source_health "
                "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM source_health);"
            )
            row = cur.fetchone()
            if row:
                db_sources = int(row["n"])
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded",
                     "db_error": f"{type(e).__name__}: {e}",
                     "diff_available": diff_available,
                     "diff": diff if diff_available else None},
        )

    failed = diff.get("failed_scrapers") or []
    added = diff.get("added") or []
    removed = diff.get("removed") or []
    zeroed = diff.get("zeroed") or []
    fc = diff.get("fatf_changes") or {}
    return {
        "status": "ok",
        "db_target": DB_TARGET,
        "diff_available": diff_available,
        "last_scrape_run": diff.get("generated_at"),
        "last_scrape_delta": diff.get("delta_total"),
        "sources_refreshed": len(added),
        "sources_lost_rows": len(removed),
        "sources_zeroed": len(zeroed),
        "sources_failed": len(failed),
        "failed_scrapers": failed[:20],
        "fatf_changes": fc,
        "db_records": db_records,
        "db_sources": db_sources,
        "screening_api_version": "1.1.0",
        "screening_api_tests": "69/69",
        "summary": diff.get("summary") or [],
    }


@app.get("/api/health")
def health():
    """Use pg_stat_user_tables.n_live_tup for row counts — it's a planner
    estimate maintained by autovacuum, accurate within ~1% and returns
    instantly. COUNT(*) on a 6M-row table over RDS takes 20s."""
    try:
        with cursor() as cur:
            cur.execute(
                """
                SELECT relname, n_live_tup
                FROM pg_stat_user_tables
                WHERE relname IN ('watchlist_records', 'rbi_odi_investments');
                """
            )
            counts = {r["relname"]: int(r["n_live_tup"]) for r in cur.fetchall()}
        return {
            "status": "ok",
            "db_target": DB_TARGET,
            "watchlist_records": counts.get("watchlist_records", 0),
            "rbi_odi_investments": counts.get("rbi_odi_investments", 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unavailable: {e}")


@app.post("/api/screen", dependencies=[Depends(verify_api_key)])
def screen(req: ScreenRequest, request: Request):
    with cursor() as cur:
        out = _screen_one(cur, req.name, req.threshold, req.max_results)
    _log_event("SCREEN", _client_ip(request),
               name=req.name, risk=out["risk_level"],
               matches=out["total_matches"],
               time_ms=out["screening_time_ms"])
    return out


@app.post("/api/screen/bulk", dependencies=[Depends(verify_api_key)])
def screen_bulk(req: BulkScreenRequest, request: Request):
    t0 = time.perf_counter()
    results = []
    with cursor() as cur:
        for item in req.names:
            results.append(_screen_one(cur, item.name, req.threshold, req.max_results))
    total_ms = int((time.perf_counter() - t0) * 1000)
    _log_event("BULK", _client_ip(request),
               names=len(results), time_ms=total_ms,
               risks=[r["risk_level"] for r in results])
    return {"results": results, "total_screened": len(results),
            "total_time_ms": total_ms}


@app.get("/api/screen/report/{query}", response_class=HTMLResponse,
         dependencies=[Depends(verify_api_key)])
def screen_report(
    request: Request,
    query: str = Path(..., min_length=1, max_length=500),
    threshold: float = Query(DEFAULT_THRESHOLD, ge=0.1, le=1.0),
    max_results: int = Query(DEFAULT_MAX_RESULTS, ge=1, le=200),
):
    query = _sanitize_name(query)
    if len(query) < 2:
        raise HTTPException(status_code=422, detail="query too short after sanitization")
    with cursor() as cur:
        result = _screen_one(cur, query, threshold, max_results)
    _log_event("REPORT", _client_ip(request),
               name=query, risk=result["risk_level"],
               matches=result["total_matches"])
    return HTMLResponse(_render_html_report(result))


@app.get("/api/sources", dependencies=[Depends(verify_api_key)])
def sources(
    request: Request,
    min_count: int = Query(1, ge=0),
):
    with cursor() as cur:
        out = _sources_cached(cur, min_count)
    _log_event("SOURCES", _client_ip(request),
               total_sources=out["total_sources"], cached_at=out["cached_at"])
    return out


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
    q = html.escape(r["query"], quote=True)
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
            f"<li>{html.escape(t.get('indian_party') or '')} &rarr; "
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
            f"<p><strong>{html.escape(fatf['list']).upper()} LIST</strong>: "
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
