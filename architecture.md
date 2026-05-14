# Architecture — Risk Pipeline v2

**Project:** Risk Pipeline
**Document type:** System architecture, data flow, and design rationale
**Audience:** Engineers, Claude Code, CTO
**Last updated:** 2026-05-06

---

## 1. Purpose

This document defines the system's structure, data flow, component boundaries, and design decisions. It is authoritative — any change that contradicts this document must update it first and gain explicit approval.

---

## 2. Design Philosophy

**Classification-first architecture.** The system classifies source URLs by type before scraping. Different types route to different engines. This enables mass scraping across hundreds of sources without per-source custom code for every one.

**Good-enough generic extraction.** Generic scraper engines extract structured data from most sources automatically. Quality varies by source complexity. High-value sources can optionally receive custom scrapers for precise extraction.

**Graceful degradation.** Sources that cannot be scraped (login, captcha, dead URLs) are logged and skipped. The pipeline never fails because one source is inaccessible.

**Configuration-driven.** All sources are defined in sources.json. Adding a new source is a configuration change. The framework code does not change.

**No silent failures.** Every action is logged. Every failure triggers an alert. Sanity checks fail loudly.

---

## 3. System Overview

```
+-------------------------------------------------------------------+
|                                                                     |
|  sources.json (244 India sources, expandable to UK/USA)            |
|       |                                                             |
|       v                                                             |
|  classify.py --- visits each URL, determines type                  |
|       |                                                             |
|  +----+----+--------+-----------+----------+                       |
|  v    v    v        v           v          v                       |
| html  pdf  js    restricted   dead     custom                     |
| urls  urls urls    urls       urls     (6 existing)               |
|  |    |    |        |          |          |                        |
|  v    v    v        v          v          v                        |
| html_ pdf_ js_    log &     log &     scrapers/                   |
| scr.  scr. scr.   skip      skip     <source>.py                 |
|  |    |    |                            |                          |
|  +----+----+----------------------------+                          |
|       |                                                             |
|       v                                                             |
|  data/<source>.csv  (per-source output)                            |
|       |                                                             |
|       v                                                             |
|  combine.py --> data/master_watchlist.csv                          |
|       |                                                             |
|       v                                                             |
|  generate_tracker.py --> project_status.xlsx                       |
|       |                                                             |
|       v                                                             |
|  alerter.py --> Telegram / stderr summary                          |
|                                                                     |
+-------------------------------------------------------------------+
```

---

## 4. Component Catalog

### 4.1 sources.json (configuration)

Single source of truth. One entry per source URL. Fields:

```json
{
  "id": "unique_source_id",
  "agency": "Agency Name",
  "list_name": "Watchlist Name",
  "url": "https://...",
  "type": "html | pdf | js | restricted | dead | pending_recon",
  "scraper": "filename.py or null",
  "expected_min_records": 0,
  "status": "active | pending_recon | restricted | dead | skipped",
  "change_detection": true,
  "change_detection_selector": "table | div.class | null",
  "notes": ""
}
```

### 4.2 classify.py (URL classifier)

- Takes the canonical source list (agency + watchlist name from PPT, stored in india_watchlist_sources_complete.csv)
- For sources without URLs: attempts URL discovery on the agency's website (searches for likely paths, probes common URL patterns)
- For sources with URLs: fetches each URL once to classify
- Determines type based on: HTTP status, content-type header, HTML analysis
- Updates sources.json with discovered URL and classified type
- Runs once per new batch of sources; results are cached
- URL discovery is part of classification, not a prerequisite

### 4.3 Generic Scraper Engines

**engines/html_scraper.py**
- Processes all sources with type=html
- Strategy: find tables -> extract rows. If no tables, find repeating blocks. If nothing structured, save raw text.
- Output: one CSV per source in data/
- Does NOT attempt login, captcha bypass, or JavaScript execution

**engines/pdf_scraper.py**
- Processes all sources with type=pdf
- Strategy: download PDF -> extract tables via pdfplumber. If no tables, extract text.
- Archives PDFs in data/raw/
- Output: one CSV per source in data/

**engines/js_scraper.py (Phase 3)**
- Processes all sources with type=js
- Uses Scrapling StealthyFetcher for browser rendering
- Same extraction logic as HTML engine, applied post-render

### 4.4 Custom Scrapers (scrapers/)

Hand-written per-source extractors for high-value sources requiring precise field mapping. Currently 6:

| File | Source | Records |
|---|---|---|
| cbi_announced_rewards.py | CBI Most Wanted + /old-records | 328 |
| cbi_yellow_notices.py | CBI Yellow Notices | 94 |
| cbi_red_notices.py | CBI Red Notices | 379 |
| mha_banned_orgs.py | MHA Banned Organizations | 39 |
| mha_uapa_individual_terrorists.py | MHA UAPA Individual Terrorists | 57 |
| nia_most_wanted.py | NIA Most Wanted | 332 |

Custom scrapers take priority over generic engines. When sources.json entry has a scraper field pointing to a file in scrapers/, the handler invokes that instead of the generic engine.

### 4.5 Handlers (handlers/)

Thin dispatch layer between orchestrator and engines/scrapers:

- html_handler.py — routes to custom scraper if exists. In Phase 2, will fall back to generic HTML engine for sources without custom scrapers.
- pdf_handler.py — routes to custom scraper if exists. In Phase 2, will fall back to generic PDF engine.
- js_handler.py — placeholder (Phase 3)
- restricted_handler.py — logs and skips

NOTE: Generic engines (engines/) are Phase 2 deliverables. Currently, handlers only route to custom scrapers in scrapers/. Sources without custom scrapers are not yet scraped.

### 4.6 Orchestrator (main.py)

- Reads sources.json
- Iterates over entries
- Dispatches each to correct handler based on type
- Wraps each in try/except — one failure does not halt pipeline
- Coordinates: change detection -> scrape -> validate -> log
- After all sources: runs combine.py, generate_tracker.py, sends summary alert

### 4.7 Utilities (utils/)

- change_detector.py — SHA-256 hash of data-bearing HTML element, stored in snapshots/
- alerter.py — Telegram via urllib + stderr fallback
- logger.py — structured logging to file + stdout

### 4.8 Post-processing (scripts/)

- combine.py — merges all data/*.csv into data/master_watchlist.csv
- generate_tracker.py — produces project_status.xlsx from sources.json + run logs
- validate_cbi.py — schema and data quality validator (source-agnostic despite name)

### 4.9 Shell Orchestrator (run_all.sh)

- Activates venv, runs main.py, captures exit code
- Cron target for daily scheduled runs

---

## 5. Data Flow

### 5.1 Full pipeline run (daily via cron)

1. Cron triggers run_all.sh (or engineer runs manually)
2. main.py loads sources.json
3. For each source with status=active:
   a. Change detection: fetch, hash, compare (skip if disabled for this source)
   b. Dispatch to handler by type
   c. Handler invokes custom scraper or generic engine
   d. Engine/scraper produces data/<source_id>.csv
   e. Validator checks the CSV
   f. Result logged (success/failure/skip, record count, runtime)
4. Sources with status=pending_recon/restricted/dead: logged and skipped
5. combine.py merges all CSVs into master_watchlist.csv
6. generate_tracker.py updates project_status.xlsx
7. alerter.py sends summary

Note: classify.py is NOT part of the daily run. It runs separately when new sources are added.

### 5.2 Adding new sources (one-time per batch)

1. Add agency + watchlist names to india_watchlist_sources_complete.csv
2. Run classify.py — discovers URLs, determines type, updates sources.json
3. Sources that classify as html/pdf get status=active
4. Sources that classify as restricted/dead get appropriate status
5. Next daily pipeline run picks up newly active sources automatically
6. No framework code changes required

### 5.3 Custom scraper priority

When a source has both a custom scraper (scraper field in sources.json) and a type that a generic engine handles, the custom scraper takes priority. This allows gradual quality improvement: generic engine handles a source initially, custom scraper replaces it later when higher quality is needed.

---

## 6. Schema

### 6.1 The 17-column shared schema

Every output CSV — whether from generic engines or custom scrapers — uses these 17 columns:

source_agency, source_list, case_unit, name, father_name, date_of_birth, gender, address, reward_amount, details, has_document, document_url, detail_page_url, interpol_notice_id, link_kind, scraped_at, enrichment_status

Generic engines populate what they can extract. Custom scrapers populate all applicable fields. Empty columns are expected and acceptable.

### 6.2 link_kind taxonomy (7 values from custom scrapers)

cbi_detail_page, cbi_inline_record, cbi_document, interpol_notice, mha_banned_org, mha_uapa_individual, nia_most_wanted

Generic engines use: html_generic, pdf_generic (new values added as engines are built).

---

## 7. Key Design Decisions

### 7.1 Classification-first, not scrape-everything

Why: Trying to scrape a login-protected page wastes time and risks IP blocks. Classifying first means the system knows what it can handle before attempting anything.

### 7.2 Generic engines over per-source scrapers for coverage

Why: 244 sources at 2-4 hours of custom code each = months. Generic engines that extract tables and structured blocks cover 70-80% of sources automatically in days. Quality tradeoff is acceptable for initial coverage; high-value sources get custom scrapers later.

### 7.3 Custom scrapers coexist with generic engines

Why: Not either/or. Generic engines provide breadth. Custom scrapers provide depth. The handler layer dispatches to whichever exists. Both produce the same schema.

### 7.4 Per-type CSV grouping (html_urls, pdf_urls, etc.)

Why: Each engine processes its own type's URL list independently. This enables parallel development, independent testing, and clear ownership of extraction quality per type.

### 7.5 No database

Why: Data volume is small (tens of thousands of records). CSV is queryable with pandas, readable in Excel, portable, and requires zero infrastructure. Database adds operational cost without proportionate benefit.

### 7.6 No Docker, no cloud

Why: Single-machine deployment. Pipeline runs once daily via cron. Containerization and cloud add complexity without value at this scale.

### 7.7 Selector-scoped change detection

Why: Full-body hashing produces false positives from CSRF tokens, session IDs, timestamps. Scoping the hash to the data-bearing element (the table, card container) hashes only what the scraper actually consumes.

---

## 8. Failure Modes

| Failure | Detection | Handling |
|---|---|---|
| Source returns non-200 | Handler catches HTTP error | Logged, alerted, pipeline continues |
| Source content is empty | Sanity check in engine/scraper | Logged, alerted, CSV not written |
| Layout changed | Hash mismatch | Alerted, scrape proceeds, engineer reviews |
| Fewer records than expected | Sanity check | Alerted, CSV written with warning |
| Source requires login | Classifier detects login form | Marked restricted, skipped |
| Source is PDF | Content-type header check | Routed to PDF engine |
| Source is JS-rendered | Empty body after static fetch | Marked js, handled in Phase 3 |
| Network timeout | Fetch timeout | Logged, alerted, pipeline continues |

---

## 9. File Structure

```
risk-pipeline/
|-- sources.json                 # all 244 sources
|-- main.py                      # orchestrator
|-- classify.py                  # URL classifier (Phase 2)
|-- run_all.sh                   # cron entry point
|-- PRD.md                       # this project's requirements
|-- ARCHITECTURE.md              # this document
|-- TECH_STACK.md                # technology choices
|-- india_watchlist_sources_complete.csv  # canonical 244-source list
|
|-- engines/                     # generic type-based scrapers (Phase 2)
|   |-- html_scraper.py
|   |-- pdf_scraper.py
|   +-- js_scraper.py            # Phase 3
|
|-- scrapers/                    # custom per-source scrapers
|   |-- cbi_announced_rewards.py
|   |-- cbi_yellow_notices.py
|   |-- cbi_red_notices.py
|   |-- mha_banned_orgs.py
|   |-- mha_uapa_individual_terrorists.py
|   +-- nia_most_wanted.py
|
|-- handlers/                    # type dispatch
|   |-- html_handler.py
|   |-- pdf_handler.py
|   |-- js_handler.py
|   +-- restricted_handler.py
|
|-- utils/                       # shared infrastructure
|   |-- change_detector.py
|   |-- alerter.py
|   +-- logger.py
|
|-- scripts/                     # post-processing
|   |-- combine.py
|   |-- generate_tracker.py
|   +-- validate_cbi.py
|
|-- data/                        # output
|   |-- <source_id>.csv          # per-source
|   |-- master_watchlist.csv     # combined
|   +-- raw/                     # archived PDFs
|
|-- snapshots/                   # change detection hashes
|-- logs/                        # per-run logs
+-- project_status.xlsx          # tracker
```

---

## 10. Constraints (Hard Rules)

1. Scrapling only for HTTP/HTML. No requests, BeautifulSoup, Selenium, httpx.
2. No database. CSV and JSON only.
3. No Docker, no cloud. Single Linux machine.
4. No captcha bypass. No unauthorized logins.
5. No silent failures. Log everything, alert on failures.
6. No premature abstraction. Shared helpers duplicated until refactor justified.
7. No schema changes without updating this document.
8. No new dependencies without explicit approval.
9. One source failing never halts the pipeline.

---

## 11. Rules for Claude Code

1. Read this document and PRD.md before proposing structural changes
2. Reference specific sections when justifying decisions
3. Respect constraints in section 10
4. Generic engines: extract what's available, don't force perfect structure
5. Custom scrapers: precise field mapping, sanity checks, validation
6. When in doubt between coverage and quality, choose coverage for generic engines
7. Explain before code; show verbatim output; work in small steps
8. Do not modify existing custom scrapers without explicit approval
9. Update this document when architecture changes

---

## 12. Document History

| Date | Version | Change |
|---|---|---|
| 2026-05-05 | 1.0 | Initial — per-source custom scraper architecture |
| 2026-05-06 | 2.0 | Revised — classification-first, generic engine architecture. Added engines/ layer. Phase 1 complete. |