"""
RBI ODI REST API.

Serves the rbi_odi_investments table (~104k records, 2007-2026).

Endpoints:
    GET /api/odi/search           - filter by company / country / activity / amount / period
    GET /api/odi/company/{name}   - all records for one company (fuzzy)
    GET /api/odi/stats            - aggregate stats, top countries, top companies
    GET /api/odi/countries        - country list with counts
    GET /api/odi/export           - download all matching rows as JSON or CSV
    GET /api/health               - DB connectivity + row count

DB target is selected by env var ODI_DB:
    ODI_DB=local (default)  - local PostgreSQL
    ODI_DB=rds              - AWS RDS

Run:
    venv/bin/uvicorn api.odi_api:app --host 0.0.0.0 --port 8001 --reload
"""
import csv
import io
import os
from contextlib import contextmanager
from decimal import Decimal
from typing import Optional

import psycopg2.extras
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader, APIKeyQuery
from psycopg2.pool import ThreadedConnectionPool

# ---------------------------------------------------------------------------
# Config

DB_CONFIGS = {
    "local": dict(host="localhost", user="aayush", password="aayush123",
                  dbname="risk_pipeline"),
    "rds":   dict(host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
                  user="aayush", password="Aaayyuusshhh",
                  dbname="risk_pipeline", connect_timeout=10),
}
DB_TARGET = os.environ.get("ODI_DB", "local").lower()
if DB_TARGET not in DB_CONFIGS:
    raise RuntimeError(f"ODI_DB must be one of {list(DB_CONFIGS)}; got {DB_TARGET!r}")
DB_CONFIG = DB_CONFIGS[DB_TARGET]

# API key auth. Set ODI_API_KEY in the environment (or systemd unit) to require
# X-API-Key header / ?api_key= query param on all /api/odi/* endpoints.
# /api/health, /, and /docs stay public regardless.
API_KEY = os.environ.get("ODI_API_KEY", "")

MAX_PER_PAGE = 1000
DEFAULT_PER_PAGE = 50

# ---------------------------------------------------------------------------
# App

app = FastAPI(
    title="RBI ODI API",
    description=(
        "Reserve Bank of India - Overseas Direct Investment data "
        "(monthly press releases, 2007-2026)."
    ),
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Single connection pool reused across requests. ThreadedConnectionPool because
# FastAPI runs sync endpoints on a thread pool.
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
    """Require a valid API key on protected endpoints. Accepts X-API-Key
    header (preferred) or ?api_key= query param. If ODI_API_KEY isn't set
    on the server, all requests are rejected - fail closed rather than
    silently disabling auth."""
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


# ---------------------------------------------------------------------------
# Helpers

def _to_json(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row(row):
    return {k: _to_json(v) for k, v in dict(row).items()}


def _build_filters(
    company: Optional[str],
    jv_wos: Optional[str],
    country: Optional[str],
    activity: Optional[str],
    jv_or_wos: Optional[str],
    period_from: Optional[str],
    period_to: Optional[str],
    min_amount: Optional[float],
    max_amount: Optional[float],
    year: Optional[int] = None,
):
    """Return (where_sql, params). All filters are AND-combined."""
    clauses: list[str] = []
    params: list = []
    if company:
        clauses.append("indian_party ILIKE %s")
        params.append(f"%{company}%")
    if jv_wos:
        clauses.append("jv_wos_name ILIKE %s")
        params.append(f"%{jv_wos}%")
    if country:
        clauses.append("country ILIKE %s")
        params.append(f"%{country}%")
    if activity:
        clauses.append("activity ILIKE %s")
        params.append(f"%{activity}%")
    if jv_or_wos:
        clauses.append("UPPER(jv_or_wos) = %s")
        params.append(jv_or_wos.upper())
    if period_from:
        clauses.append(
            "TO_DATE(period_from, 'DD/MM/YYYY') >= TO_DATE(%s, 'DD/MM/YYYY')"
        )
        params.append(period_from)
    if period_to:
        clauses.append(
            "TO_DATE(period_to, 'DD/MM/YYYY') <= TO_DATE(%s, 'DD/MM/YYYY')"
        )
        params.append(period_to)
    if min_amount is not None:
        clauses.append("total_usd_mn >= %s")
        params.append(min_amount)
    if max_amount is not None:
        clauses.append("total_usd_mn <= %s")
        params.append(max_amount)
    if year is not None:
        clauses.append("SUBSTRING(period_from, 7, 4) = %s")
        params.append(str(year))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


RESULT_COLUMNS = (
    "indian_party, jv_wos_name, jv_or_wos, country, activity, "
    "equity_usd_mn, loan_usd_mn, guarantee_usd_mn, total_usd_mn, "
    "period_from, period_to, sr_no, prid, excel_filename"
)


# ---------------------------------------------------------------------------
# Endpoints

@app.get("/")
def root():
    return {
        "service": "RBI ODI API",
        "version": "1.0.0",
        "db_target": DB_TARGET,
        "endpoints": [
            "/api/health",
            "/api/odi/search",
            "/api/odi/company/{name}",
            "/api/odi/stats",
            "/api/odi/countries",
            "/api/odi/export",
            "/docs",
        ],
    }


@app.get("/api/health")
def health():
    try:
        with cursor() as cur:
            cur.execute("SELECT COUNT(*) AS rows FROM rbi_odi_investments;")
            total = cur.fetchone()["rows"]
        return {
            "status": "ok",
            "db_target": DB_TARGET,
            "table": "rbi_odi_investments",
            "rows": total,
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unavailable: {e}")


@app.get("/api/odi/search", dependencies=[Depends(verify_api_key)])
def search(
    company: Optional[str] = Query(None, description="ILIKE on indian_party"),
    jv_wos: Optional[str] = Query(None, description="ILIKE on jv_wos_name"),
    country: Optional[str] = Query(None, description="ILIKE on country"),
    activity: Optional[str] = Query(None, description="ILIKE on activity"),
    jv_or_wos: Optional[str] = Query(None, description="JV or WOS"),
    period_from: Optional[str] = Query(None, description="DD/MM/YYYY lower bound"),
    period_to: Optional[str] = Query(None, description="DD/MM/YYYY upper bound"),
    min_amount: Optional[float] = Query(None),
    max_amount: Optional[float] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1, le=MAX_PER_PAGE),
):
    where, params = _build_filters(
        company, jv_wos, country, activity, jv_or_wos,
        period_from, period_to, min_amount, max_amount,
    )

    with cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM rbi_odi_investments {where};", params)
        total = cur.fetchone()["total"]

        offset = (page - 1) * per_page
        cur.execute(
            f"""
            SELECT {RESULT_COLUMNS}
            FROM rbi_odi_investments
            {where}
            ORDER BY TO_DATE(period_from, 'DD/MM/YYYY') DESC NULLS LAST,
                     indian_party, sr_no
            LIMIT %s OFFSET %s;
            """,
            params + [per_page, offset],
        )
        results = [_row(r) for r in cur.fetchall()]

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "results": results,
    }


@app.get("/api/odi/company/{name}", dependencies=[Depends(verify_api_key)])
def company_detail(
    name: str = Path(..., min_length=2),
    exact: bool = Query(False, description="If true, exact (case-insensitive) match"),
):
    with cursor() as cur:
        if exact:
            clause = "UPPER(indian_party) = UPPER(%s)"
            param = name
        else:
            clause = "indian_party ILIKE %s"
            param = f"%{name}%"

        cur.execute(
            f"""
            SELECT {RESULT_COLUMNS}
            FROM rbi_odi_investments
            WHERE {clause}
            ORDER BY TO_DATE(period_from, 'DD/MM/YYYY') DESC NULLS LAST, sr_no;
            """,
            [param],
        )
        rows = [_row(r) for r in cur.fetchall()]

    if not rows:
        raise HTTPException(status_code=404, detail=f"no records for company {name!r}")

    total_usd = sum(r["total_usd_mn"] or 0 for r in rows)
    countries = sorted({r["country"] for r in rows if r["country"]})
    return {
        "company": name,
        "match_kind": "exact" if exact else "fuzzy",
        "records": len(rows),
        "total_usd_mn": round(float(total_usd), 4),
        "countries": countries,
        "results": rows,
    }


@app.get("/api/odi/stats", dependencies=[Depends(verify_api_key)])
def stats(
    company: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    year: Optional[int] = Query(None, ge=2007, le=2030),
):
    where, params = _build_filters(
        company=company, jv_wos=None, country=country, activity=None,
        jv_or_wos=None, period_from=None, period_to=None,
        min_amount=None, max_amount=None, year=year,
    )

    with cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) AS total_records,
                   COALESCE(SUM(total_usd_mn), 0) AS total_usd_mn,
                   COUNT(DISTINCT indian_party) AS unique_companies,
                   COUNT(DISTINCT country) AS unique_countries,
                   MIN(TO_DATE(period_from, 'DD/MM/YYYY')) AS min_period,
                   MAX(TO_DATE(period_to, 'DD/MM/YYYY')) AS max_period
            FROM rbi_odi_investments
            {where};
            """,
            params,
        )
        agg = cur.fetchone()

        cur.execute(
            f"""
            SELECT country,
                   COUNT(*) AS records,
                   COALESCE(SUM(total_usd_mn), 0) AS total_usd_mn
            FROM rbi_odi_investments
            {where}
            GROUP BY country
            ORDER BY records DESC
            LIMIT 10;
            """,
            params,
        )
        top_countries = [_row(r) for r in cur.fetchall()]

        cur.execute(
            f"""
            SELECT indian_party AS company,
                   COUNT(*) AS records,
                   COALESCE(SUM(total_usd_mn), 0) AS total_usd_mn
            FROM rbi_odi_investments
            {where}
            GROUP BY indian_party
            ORDER BY total_usd_mn DESC
            LIMIT 10;
            """,
            params,
        )
        top_companies = [_row(r) for r in cur.fetchall()]

    return {
        "filters": {"company": company, "country": country, "year": year},
        "total_records": agg["total_records"],
        "total_usd_mn": round(float(agg["total_usd_mn"]), 4),
        "unique_companies": agg["unique_companies"],
        "unique_countries": agg["unique_countries"],
        "period_range": {
            "from": agg["min_period"].strftime("%d/%m/%Y") if agg["min_period"] else None,
            "to":   agg["max_period"].strftime("%d/%m/%Y") if agg["max_period"] else None,
        },
        "top_countries": top_countries,
        "top_companies": top_companies,
    }


@app.get("/api/odi/countries", dependencies=[Depends(verify_api_key)])
def countries():
    with cursor() as cur:
        cur.execute(
            """
            SELECT country,
                   COUNT(*) AS records,
                   COALESCE(SUM(total_usd_mn), 0) AS total_usd_mn
            FROM rbi_odi_investments
            WHERE country IS NOT NULL AND country != ''
            GROUP BY country
            ORDER BY records DESC;
            """
        )
        rows = [_row(r) for r in cur.fetchall()]
    return {"count": len(rows), "countries": rows}


@app.get("/api/odi/export", dependencies=[Depends(verify_api_key)])
def export(
    format: str = Query("json", pattern="^(json|csv)$"),
    company: Optional[str] = Query(None),
    jv_wos: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    activity: Optional[str] = Query(None),
    jv_or_wos: Optional[str] = Query(None),
    period_from: Optional[str] = Query(None),
    period_to: Optional[str] = Query(None),
    min_amount: Optional[float] = Query(None),
    max_amount: Optional[float] = Query(None),
):
    where, params = _build_filters(
        company, jv_wos, country, activity, jv_or_wos,
        period_from, period_to, min_amount, max_amount,
    )
    sql = f"""
        SELECT {RESULT_COLUMNS}
        FROM rbi_odi_investments
        {where}
        ORDER BY TO_DATE(period_from, 'DD/MM/YYYY') NULLS LAST, sr_no;
    """

    if format == "csv":
        def stream_csv():
            buf = io.StringIO()
            writer = csv.writer(buf)
            cols = [c.strip() for c in RESULT_COLUMNS.split(",")]
            writer.writerow(cols)
            yield buf.getvalue()
            buf.seek(0); buf.truncate()

            with cursor(dict_rows=False) as cur:
                cur.execute(sql, params)
                while True:
                    batch = cur.fetchmany(1000)
                    if not batch:
                        break
                    for row in batch:
                        writer.writerow([
                            float(v) if isinstance(v, Decimal) else
                            ("" if v is None else v)
                            for v in row
                        ])
                    yield buf.getvalue()
                    buf.seek(0); buf.truncate()

        return StreamingResponse(
            stream_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=rbi_odi.csv"},
        )

    with cursor() as cur:
        cur.execute(sql, params)
        rows = [_row(r) for r in cur.fetchall()]
    return {"count": len(rows), "results": rows}
