# Product Requirements Document — Risk Pipeline v2

**Project:** Indian AML Watchlist Mass Scraping Automation
**Owner:** Aayush, Engineer — Scoreme Solutions / Resurgent India
**Status:** Phase 1 Complete. Phase 2 (Mass Classification + Generic Scraping) starting.
**Last updated:** 2026-05-06

---

## 1. Executive Summary

Risk Pipeline is a mass scraping automation system that ingests Indian regulatory watchlists at scale. It classifies source URLs by type (HTML, PDF, JS-rendered, restricted), routes them to type-specific scraper engines, extracts structured data, and produces a unified master dataset for AML compliance screening.

The system covers 244 Indian regulatory sources across 70+ agencies. It runs from a single command, handles different source types automatically, skips what it cannot access, and reports status honestly via an auto-generated tracker spreadsheet.

Built for Scoreme Solutions / Resurgent India to support KYC, credit-decision, and AML compliance workflows for banks and NBFCs.

---

## 2. Problem Statement

Indian financial institutions must screen counterparties against regulatory watchlists from agencies including CBI, MHA, NIA, ED, SEBI, RBI, MCA, state police, stock exchanges, PSUs, and others. There are 244 such watchlists across India (per ZIGRAM PreScreening.io reference).

These lists are published in inconsistent formats (static HTML tables, card layouts, PDFs, JS-rendered pages, login-protected portals) across dozens of government websites with varying structures, update schedules, and technical accessibility.

No single unified dataset exists. Commercial alternatives are expensive and not India-specific.

---

## 3. Product Vision

A mass scraping automation system that:

- Accepts a list of source URLs as input
- Automatically classifies each URL by source type (HTML, PDF, JS, restricted, dead)
- Routes classified URLs to type-specific scraper engines
- Extracts structured data from each source in a standardized format
- Produces per-source CSVs and a combined master watchlist CSV
- Generates a tracker spreadsheet showing completed/not-completed status per source
- Detects when source website layouts change via content hashing
- Sends alerts on failures or layout changes
- Runs unattended on a Linux server via cron
- Handles new URLs the same way — whether 1, 5, or 50 — classify, route, scrape

The system is designed for coverage and speed, not per-source perfection. Generic type-based engines handle most sources automatically. Sources requiring special handling can optionally receive custom scraper code.

---

## 4. System Architecture (High Level)

```
Input: source URLs (from PPT / sources.json / manual addition)
              |
              v
       classify.py
       (visits each URL, determines type)
              |
       +------+------+----------+------------+
       v      v      v          v            v
    html    pdf     js       restricted     dead
    urls    urls    urls      urls          urls
       |      |      |          |            |
       v      v      v          v            v
   html_    pdf_   js_       log &        log &
   scraper  scraper scraper  skip         skip
       |      |      |
       v      v      v
   per-source CSVs (structured data)
              |
              v
       combine.py -> master_watchlist.csv
              |
              v
       tracker.xlsx (completed / not completed)
```

---

## 5. Source Types and Handling Strategy

| Type | Description | Engine | Expected count |
|---|---|---|---|
| HTML (tables) | Page contains <table> with data rows | Generic HTML table extractor | ~100-120 |
| HTML (cards/blocks) | Page has repeating div blocks with fields | HTML extractor with block detection | ~30-40 |
| PDF | Data published as downloadable PDF | Generic PDF table extractor via pdfplumber | ~30-40 |
| JS-rendered | Content loads via JavaScript after page render | StealthyFetcher / PlayWrightFetcher (Phase 3) | ~20-30 |
| Restricted | Requires login, captcha, or subscription | Skip with logged reason | ~15-25 |
| Dead/moved | URL returns 404, 500, or redirects to unrelated page | Skip with logged reason | ~10-15 |

The system handles all types gracefully. Active types produce data. Restricted and dead types are logged and skipped without failure.

---

## 6. Functional Requirements

### 6.1 URL Classification (classify.py)

- Accepts the canonical source list (agency name + watchlist name from PPT)
- For sources without URLs: attempts to discover the correct URL on the agency's website
- For sources with URLs: fetches the page and determines its type based on: HTTP status code, response content type, HTML content analysis (tables, login forms, JS framework markers, captcha elements)
- Outputs categorized entries with type field updated in sources.json
- Classification runs once per new batch of sources; results cached in sources.json
- URL discovery is part of classification, not a prerequisite

### 6.2 Generic HTML Scraper Engine (html_scraper.py)

- Reads sources with type=html from sources.json
- For each URL: fetches page via Scrapling Fetcher, finds all <table> elements, extracts all rows into structured CSV, if no tables found attempts block structures (cards, definition lists), if nothing structured found saves raw text with "unstructured" flag
- Saves output to data/<source_id>.csv
- Processes one source at a time with 2-second politeness delay
- Per-source timeout: 5 minutes

### 6.3 Generic PDF Scraper Engine (pdf_scraper.py)

- Reads sources with type=pdf from sources.json
- Downloads PDF to data/raw/<source_id>.pdf
- Extracts tables via pdfplumber extract_tables()
- If no tables, extracts raw text via extract_text()
- Saves structured output to data/<source_id>.csv

### 6.4 JS Scraper Engine (Phase 3 — js_scraper.py)

- Placeholder in Phase 2; fully implemented in Phase 3
- Uses Scrapling StealthyFetcher or PlayWrightFetcher
- Same extraction logic as HTML engine, applied after JavaScript execution

### 6.5 Restricted/Dead Handler

- Sources classified as restricted or dead are logged with reason
- No scraping attempted; status recorded in sources.json and tracker
- Restricted reasons: login required, captcha present, subscription needed

### 6.6 Custom Scrapers (existing + future)

- For high-value sources requiring precise extraction, custom scrapers exist in scrapers/
- Currently 6 custom scrapers: CBI Announced Rewards, CBI Yellow Notices, CBI Red Notices, MHA Banned Orgs, MHA UAPA, NIA Most Wanted
- Custom scrapers take priority over generic engines when both exist
- New custom scrapers can be added without modifying the framework

### 6.7 Data Combination (combine.py)

- Merges all per-source CSVs into data/master_watchlist.csv
- Validates schema consistency; reports total and per-source counts

### 6.8 Tracker Spreadsheet (generate_tracker.py)

- Produces project_status.xlsx with one row per source (all 244)
- Columns: ppt_number, agency, watchlist_details, type, status, records, last_run, notes
- Status values: completed, pending_recon, restricted, dead, skipped
- Auto-generated after each pipeline run

### 6.9 Change Detection

- SHA-256 hash of data-bearing HTML element (scoped via selector to avoid false positives)
- Compared against stored hash from previous run in snapshots/<source_id>.hash
- Changed -> alert sent, scrape proceeds, engineer reviews
- PDF sources: change detection disabled (URL changes per gazette)

### 6.10 Alerts and Notifications

- Telegram bot (primary) or stderr fallback
- Triggers: failure, layout change, record count anomaly, pipeline summary

### 6.11 Logging

- Per-run log: logs/run_YYYY-MM-DD_HH-MM.log
- Format: timestamp, level, source_id, action, detail

### 6.12 Scheduling

- run_all.sh invoked by cron daily
- Daily pipeline flow: scrape active sources -> validate -> combine -> track -> alert
- classify.py runs separately — once when new URLs are added, not on every daily run. Classification results are cached in sources.json.

---

## 7. Data Schema

All output conforms to a shared 17-column CSV format:

| # | Column | Description |
|---|---|---|
| 1 | source_agency | Issuing agency name |
| 2 | source_list | Specific watchlist name |
| 3 | case_unit | Case ID or branch identifier |
| 4 | name | Full name including aliases (@ separated) |
| 5 | father_name | Parent name where published |
| 6 | date_of_birth | ISO YYYY-MM-DD or empty |
| 7 | gender | Male / Female / Other / empty |
| 8 | address | Address as published |
| 9 | reward_amount | Integer INR amount or empty |
| 10 | details | Charges, status, organization |
| 11 | has_document | Yes / No |
| 12 | document_url | URL to source document |
| 13 | detail_page_url | URL to detail page |
| 14 | interpol_notice_id | Interpol notice ID |
| 15 | link_kind | Source pattern type tag |
| 16 | scraped_at | Timestamp |
| 17 | enrichment_status | none / partial / full |

Generic engines populate columns they can extract; others left empty. Custom scrapers populate all applicable fields.

---

## 8. Phase Plan

| Phase | Duration | Deliverable | Status |
|---|---|---|---|
| Phase 1: Framework | 3 days | Orchestrator, handlers, change detection, alerts, logging, combiner, tracker, run_all.sh. 6 custom scrapers, 1,229 records. | DONE |
| Phase 2: Mass Classification + Generic Scraping | 5 days | classify.py categorizes all 244 URLs. Generic HTML and PDF engines scrape 150+ sources. Master CSV with thousands of records. | NEXT |
| Phase 3: JS sources + quality refinement | 1-2 weeks | StealthyFetcher for JS sources. Improve extraction quality. Add custom scrapers for high-value sources. | Planned |
| Phase 4: Screening tool (optional) | 4-8 weeks | Name-matching CLI/API. Fuzzy matching, risk scoring, bulk screening. | Pending CTO decision |

---

## 9. Success Criteria

- All 244 India sources classified by type within Day 1 of Phase 2
- 150+ sources producing structured data within Day 5 of Phase 2
- Tracker shows honest status for every source
- Pipeline runs end-to-end from single command
- Alerts fire on failures and layout changes
- Master CSV combines all sources into one queryable file
- System handles new URLs (UK, USA) without architectural changes

---

## 10. Constraints

- Scrapling only for HTTP/HTML (CTO directive)
- pdfplumber for PDF extraction
- No database (CSV and JSON only)
- No Docker, no cloud (single Linux machine)
- No captcha bypass services
- No unauthorized logins
- Restricted sources require authorized credentials (pending MCA/legal contact)

---

## 11. Open Items

- MCA portal access: pending legal contact conversation
- CIBIL access: likely requires commercial subscription
- UK and USA sources: after India coverage is stable
- Screening tool: pending CTO decision

---

## 12. Rules for Claude Code

1. Read this document and ARCHITECTURE.md before structural changes
2. Generic engines produce "good enough" extraction — do not over-engineer per source
3. Respect the phase plan — do not jump phases
4. No new dependencies without explicit approval
5. Sources that cannot be scraped are logged and skipped, never forced
6. Tracker must always reflect honest current status
7. All output conforms to the 17-column schema
8. One source failing must never halt the pipeline
9. Explain before code; show verbatim output; work in small steps

---

## 13. Document History

| Date | Version | Change |
|---|---|---|
| 2026-05-05 | 1.0 | Initial PRD — per-source custom scraper approach |
| 2026-05-06 | 2.0 | Revised — classification-based generic engine approach. Phase 1 complete. |