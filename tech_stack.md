# Tech Stack — Risk Pipeline v2

**Project:** Risk Pipeline
**Document type:** Technology choices, rationale, and constraints
**Audience:** Engineers, Claude Code, infrastructure setup
**Last updated:** 2026-05-06

---

## 1. Purpose

Defines exact technologies, versions, and what is explicitly excluded. Any addition requires explicit engineer approval and an update to this document.

---

## 2. Runtime Environment

| Component | Version | Purpose |
|---|---|---|
| OS | Ubuntu 22.04+ (Linux) | Production environment |
| Python | 3.12 | Programming language |
| Virtual environment | venv/ (built-in) | Dependency isolation |
| Scheduling | cron (system) | Daily automated runs |
| Shell | bash | Orchestrator script (run_all.sh) |

---

## 3. Core Libraries

### 3.1 Scrapling (0.4.7)

Purpose: HTTP fetching and HTML parsing for all scraper engines.

Provides: Fetcher.get(url), CSS selectors, DOM traversal, anti-bot User-Agent, response handling.

Used by: classify.py, all generic engines, all custom scrapers, change_detector.py.

Phase 3 addition: StealthyFetcher / PlayWrightFetcher for JS-rendered pages (same library, different fetcher class).

### 3.2 pdfplumber (0.11.9)

Purpose: PDF text and table extraction.

Used by: Generic PDF engine (engines/pdf_scraper.py), MHA UAPA custom scraper.

### 3.3 pandas (2.x)

Purpose: CSV manipulation, validation, data combination.

Used by: scripts/validate_cbi.py, scripts/combine.py, scripts/generate_tracker.py, generic engines for DataFrame operations.

### 3.4 openpyxl (3.1.5)

Purpose: Excel file generation for tracker spreadsheet.

Used by: scripts/generate_tracker.py (via pandas to_excel).

---

## 4. Standard Library Modules

| Module | Purpose |
|---|---|
| csv | CSV reading/writing in scrapers and engines |
| json | sources.json, configuration |
| re | Regex for cleaning, classification |
| datetime | Timestamps, date validation |
| hashlib | SHA-256 for change detection |
| os, pathlib | File path handling |
| sys | Script entry points, exit codes |
| urllib.parse | URL handling |
| urllib.request | Telegram API POST (alerter) |
| logging | Structured logging |
| subprocess | Running scripts from main.py |
| importlib | Dynamic scraper module loading |

---

## 5. Full requirements.txt

```
scrapling==0.4.7
pdfplumber==0.11.9
pandas>=2.0.0
openpyxl>=3.1.0
```

Four external dependencies. Everything else is standard library.

---

## 6. Notifications

### 6.1 Telegram bot (primary)

- Bot token and chat ID in environment variables
- POST to Telegram Bot API via urllib (no SDK)
- Push alerts to engineer's phone

### 6.2 Stderr fallback

- When Telegram env vars not set, alerts print to stderr
- Format: ALERT [severity]: message

### 6.3 Email (stub)

- Function signature exists in alerter.py
- Not implemented yet; Telegram is sufficient for current scale

---

## 7. Storage

| Data | Location |
|---|---|
| Source configuration | sources.json |
| Per-source scraped data | data/<source_id>.csv |
| Combined master output | data/master_watchlist.csv |
| Archived PDFs | data/raw/ |
| Run logs | logs/run_YYYY-MM-DD_HH-MM.log |
| Change detection hashes | snapshots/<source_id>.hash |
| Tracker spreadsheet | project_status.xlsx |
| Canonical source list | india_watchlist_sources_complete.csv |

No database. All persistence via CSV, JSON, and flat files.

---

## 8. Excluded Technologies

### Permanently excluded (never add without CTO approval)

| Technology | Why excluded |
|---|---|
| requests, httpx, aiohttp | Scrapling handles all HTTP. CTO directive. |
| BeautifulSoup / bs4, lxml, parsel | Scrapling handles all HTML parsing. |
| SQLite, PostgreSQL, MongoDB | No database needed at this scale. |
| Docker, Kubernetes | Single-machine deployment. |
| AWS, GCP, Azure SDKs | No cloud. |
| Airflow, Prefect, Dagster | Cron + bash sufficient. |
| YAML, TOML libraries | JSON sufficient. |

### Excluded for now (may add in specific phases)

| Technology | When it may be added |
|---|---|
| Selenium, Playwright (direct) | Phase 3: JS engine uses Scrapling's StealthyFetcher instead |
| rapidfuzz | Phase 4: screening tool fuzzy matching |
| spacy, nltk (NER) | If Press Release text mining is greenlit |
| Flask, FastAPI | Phase 4: if screening API is built |
| scikit-learn, transformers | Only if rule-based matching proves insufficient |

---

## 9. Setup Instructions

### 9.1 Initial setup

```bash
cd risk-pipeline
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 9.2 Environment variables (for alerts)

```bash
export TELEGRAM_BOT_TOKEN="<token>"
export TELEGRAM_CHAT_ID="<chat_id>"
```

### 9.3 Run pipeline

```bash
# Single run
./run_all.sh

# Schedule daily at 6 AM
crontab -e
# Add: 0 6 * * * /home/aayush/risk-pipeline/run_all.sh
```

---

## 10. Rules for Claude Code

1. Never pip install without explicit engineer approval
2. Never use excluded libraries (section 8)
3. Use Scrapling Fetcher for all HTTP/HTML
4. Use standard library wherever possible
5. Pin new dependencies with exact versions
6. Update this document when dependencies change
7. Confirm Python 3.12 compatibility for any new library

---

## 11. Document History

| Date | Version | Change |
|---|---|---|
| 2026-05-05 | 1.0 | Initial tech stack |
| 2026-05-06 | 2.0 | Updated to reflect Phase 1 completion, openpyxl added, engines/ layer planned. Clarified excluded-for-now vs permanently excluded. |