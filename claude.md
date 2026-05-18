# Claude Code Prompt — May 18, 2026 (Pull-Forward Tasks)

> Copy-paste this entire block into Claude Code. Two independent tasks. Do them in order. Read all instructions before starting.

---

## CONTEXT (read first, do not skip)

**Project:** Overwatch AML Intelligence Platform
**Location:** `/home/aayush/risk-pipeline/`
**Database:** PostgreSQL — `database=risk_pipeline`, `user=aayush`, `password=aayush123`
**Python venv:** `/home/aayush/risk-pipeline/venv` (Python 3.12)
**Table:** `watchlist_records` — 4,706,630 rows, 601 active sources

**Actual table schema (use ONLY these columns — do NOT assume others):**
```
id, source_id, source_agency, source_list, case_unit, name, father_name,
date_of_birth, gender, address, reward_amount, details, has_document,
document_url, detail_page_url, interpol_notice_id, link_kind, scraped_at,
enrichment_status, loaded_at
```

**Master source registry:** `/home/aayush/risk-pipeline/sources.json`
- This is a JSON array of all 609+ source objects
- Each source has: `id`, `name`, `agency`, `url`, `method`, `active` (true/false), `country`, and other fields
- `source_id` in `watchlist_records` maps to `id` in `sources.json`
- Examine the actual structure of `sources.json` before writing any code that reads it

**Existing scripts directory:** `/home/aayush/risk-pipeline/scripts/`
**Existing logs directory:** `/home/aayush/risk-pipeline/logs/` (create if missing)

**ScoreMe MCA API credentials (tested and working):**
- clientId: `c07339b56ae74975d778445e23d46500`
- clientSecret: `cf73ee3eaacb0201dd1a4166e5d1ac744c32c6605fd14c3188f951ed4c6384fc`
- API 1 (Company Basic Details — FAST, use this one):
  - POST `https://quality-da-proxy.scoreme.in/mca/external/companyBasicDetails`
  - Headers: `Content-Type: application/json`
  - Auth: `clientId` and `clientSecret` go in the JSON body alongside `cin_llpin`
  - Body: `{"cin_llpin": "CIN_HERE", "clientId": "...", "clientSecret": "..."}`
  - Returns: `companyName`, `companyStatus`, `registeredAddress`, `whetherCompanyDefaulter`,
    `whetherVanishingCompanyYN`, `whetherCompanyDormantCompany`, `dateOfIncorporation`,
    `authorisedCapital`, `paidUpCapital`, `emailId`

**IMPORTANT CONSTRAINTS:**
- Always activate venv first: `source /home/aayush/risk-pipeline/venv/bin/activate`
- Install any missing pip packages with `pip install <pkg> --break-system-packages`
- Do NOT modify `watchlist_records` table structure without asking
- Do NOT delete or truncate any existing data
- All new scripts go in `/home/aayush/risk-pipeline/scripts/`
- All logs go in `/home/aayush/risk-pipeline/logs/`
- Use `PGPASSWORD=aayush123 psql -U aayush -d risk_pipeline` for any SQL

---

## TASK 1: Source Health Monitor + Layout Change Detection + Slack Alerts

**Goal:** Build `scripts/monitor_sources.py` — a production monitoring system that detects broken sources, row count anomalies, and stale scrapes across all 601 active sources. Sends Slack alerts for anything flagged.

### Step 1A: Examine existing files first

Before writing any code:
1. Read `sources.json` — understand the exact JSON structure (keys, nesting, what `active` looks like)
2. Run: `SELECT source_id, source_agency, source_list, COUNT(*) as row_count, MAX(scraped_at) as last_scraped FROM watchlist_records GROUP BY source_id, source_agency, source_list ORDER BY row_count DESC LIMIT 20;`
3. Run: `SELECT COUNT(DISTINCT source_id) FROM watchlist_records;`
4. Check if `/home/aayush/risk-pipeline/.claude/settings.local.json` exists and has a Slack webhook URL
5. Check what's already in `/home/aayush/risk-pipeline/scripts/` — don't duplicate existing functionality

### Step 1B: Build the baseline snapshot system

Create a table `source_health` in PostgreSQL to store daily snapshots:
```sql
CREATE TABLE IF NOT EXISTS source_health (
    id SERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_agency TEXT,
    source_list TEXT,
    row_count INTEGER NOT NULL DEFAULT 0,
    last_scraped TIMESTAMP,
    snapshot_date DATE NOT NULL DEFAULT CURRENT_DATE,
    status TEXT DEFAULT 'OK',  -- OK, BROKEN, STALE, ANOMALY
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_health_source ON source_health (source_id);
CREATE INDEX IF NOT EXISTS idx_health_date ON source_health (snapshot_date);
```

### Step 1C: Build `scripts/monitor_sources.py`

The script must:

1. **Snapshot current state:** For every distinct `source_id` in `watchlist_records`, record `row_count` and `last_scraped` into `source_health` for today's date (upsert — if already run today, update).

2. **Compare with previous snapshot:** Get yesterday's (or most recent prior) snapshot. For each source, detect:
   - **BROKEN:** Row count dropped to 0 when it was >0 before
   - **ANOMALY:** Row count dropped by >50% compared to previous snapshot
   - **STALE:** `last_scraped` is older than 7 days (source hasn't been refreshed)
   - **NEW:** Source exists today but not in any previous snapshot (newly added)
   - **OK:** No issues detected

3. **Generate a report:** Write to `logs/monitor_report_YYYYMMDD.txt` with:
   - Summary: total sources, OK count, broken count, anomaly count, stale count, new count
   - Detailed list of every flagged source (not OK) with: source_id, agency, list name, previous count, current count, change %, last scraped, status
   - Top 10 sources by row count (for quick reference)
   - Bottom 10 sources by row count (likely low-quality or broken)

4. **Slack alerts:** If ANY source is BROKEN or ANOMALY:
   - Look for Slack webhook URL in `/home/aayush/risk-pipeline/.claude/settings.local.json` (parse the JSON, look for any key containing "slack" or "webhook")
   - If no webhook found, check environment variable `SLACK_WEBHOOK_URL`
   - If still no webhook found, log a warning but don't crash — just skip Slack
   - Send a single consolidated Slack message with all flagged sources:
     ```
     🚨 AML Source Monitor Alert — {date}
     BROKEN: {count} sources (row count dropped to 0)
     ANOMALY: {count} sources (row count dropped >50%)
     STALE: {count} sources (not scraped in 7+ days)

     Flagged sources:
     • {source_agency} / {source_list} — {status} (was {old_count} → now {new_count})
     ...
     ```
   - Use `requests.post(webhook_url, json={"text": message})` — simple, no Slack SDK needed
   - If webhook POST fails, log the error but don't crash

5. **CLI interface:**
   - `python scripts/monitor_sources.py` — full run (snapshot + compare + report + alerts)
   - `python scripts/monitor_sources.py --snapshot-only` — just take snapshot, no comparison
   - `python scripts/monitor_sources.py --report-only` — compare last two snapshots without taking a new one
   - `python scripts/monitor_sources.py --dry-run` — do everything but don't send Slack alerts (print them to stdout instead)

6. **Logging:** Use Python `logging` module, log to both stdout and `logs/monitor.log` (append mode).

### Step 1D: Test it

1. Run with `--dry-run` first and verify the report makes sense
2. Run `--snapshot-only` to populate the first baseline
3. Verify `source_health` table has rows: `SELECT COUNT(*), snapshot_date FROM source_health GROUP BY snapshot_date;`
4. Show me the generated report file content

### Step 1E: Add to cron

Add a cron entry suggestion (don't modify crontab directly — just print the recommended line):
```
# Run source health monitor daily at 7 AM (after the 6 AM scrape completes)
0 7 * * * cd /home/aayush/risk-pipeline && /home/aayush/risk-pipeline/venv/bin/python scripts/monitor_sources.py >> logs/monitor_cron.log 2>&1
```

---

## TASK 2: MCA Company Enrichment Pipeline

**Goal:** Build `scripts/mca_enrichment.py` — queries the ScoreMe API for every unique CIN number found in `watchlist_records`, stores enrichment data in a new table, and flags high-risk companies.

### Step 2A: Examine the data first

Before writing any code:
1. Check how CIN numbers appear in the data. CINs are 21-character alphanumeric strings like `U72200MH2010PTC123456`. They might be in `details`, `case_unit`, `name`, or a dedicated field. Run:
   ```sql
   SELECT DISTINCT SUBSTRING(details FROM '[A-Z][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}') as cin
   FROM watchlist_records
   WHERE details ~ '[A-Z][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}'
   LIMIT 20;
   ```
2. Also check other columns for CIN patterns:
   ```sql
   SELECT column_name FROM information_schema.columns WHERE table_name='watchlist_records';
   ```
   Then check each text column for CIN regex matches.
3. Count total unique CINs found across all columns.
4. Test the MCA API with one real CIN from the data:
   ```bash
   curl -s -X POST https://quality-da-proxy.scoreme.in/mca/external/companyBasicDetails \
     -H "Content-Type: application/json" \
     -d '{"cin_llpin":"<A_REAL_CIN_FROM_STEP_1>","clientId":"c07339b56ae74975d778445e23d46500","clientSecret":"cf73ee3eaacb0201dd1a4166e5d1ac744c32c6605fd14c3188f951ed4c6384fc"}' | python3 -m json.tool
   ```
   Verify the response structure. Note the EXACT field names returned.

### Step 2B: Create enrichment table

```sql
CREATE TABLE IF NOT EXISTS mca_company_enrichment (
    id SERIAL PRIMARY KEY,
    cin TEXT UNIQUE NOT NULL,
    company_name TEXT,
    company_status TEXT,
    registered_address TEXT,
    date_of_incorporation TEXT,
    authorised_capital TEXT,
    paid_up_capital TEXT,
    email_id TEXT,
    is_defaulter BOOLEAN DEFAULT FALSE,
    is_vanishing BOOLEAN DEFAULT FALSE,
    is_dormant BOOLEAN DEFAULT FALSE,
    risk_level TEXT DEFAULT 'LOW',  -- HIGH, MEDIUM, LOW
    risk_flags TEXT[],              -- Array of flags like {'DEFAULTER','VANISHING'}
    raw_response JSONB,            -- Full API response for audit trail
    api_status TEXT DEFAULT 'PENDING',  -- PENDING, SUCCESS, FAILED, NOT_FOUND
    error_message TEXT,
    source_records_count INTEGER DEFAULT 0,  -- How many watchlist records reference this CIN
    first_seen_in TEXT,            -- Which source_agency first had this CIN
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mca_cin ON mca_company_enrichment (cin);
CREATE INDEX IF NOT EXISTS idx_mca_risk ON mca_company_enrichment (risk_level);
CREATE INDEX IF NOT EXISTS idx_mca_defaulter ON mca_company_enrichment (is_defaulter) WHERE is_defaulter = TRUE;
CREATE INDEX IF NOT EXISTS idx_mca_status ON mca_company_enrichment (api_status);
```

### Step 2C: Build `scripts/mca_enrichment.py`

The script must:

1. **Extract CINs:** Scan `watchlist_records` for all unique CIN numbers (use regex on all text columns). Store them in a working set.

2. **Skip already-enriched:** Check `mca_company_enrichment` for CINs with `api_status = 'SUCCESS'` — skip those. Only process PENDING, FAILED, or new CINs.

3. **Call the API (rate-limited):**
   - POST to `https://quality-da-proxy.scoreme.in/mca/external/companyBasicDetails`
   - Body: `{"cin_llpin": "<CIN>", "clientId": "c07339b56ae74975d778445e23d46500", "clientSecret": "cf73ee3eaacb0201dd1a4166e5d1ac744c32c6605fd14c3188f951ed4c6384fc"}`
   - Rate limit: **1 request per second** (use `time.sleep(1)` between calls)
   - Timeout: 30 seconds per request
   - On HTTP error or timeout: mark `api_status = 'FAILED'`, store error message, continue to next CIN
   - On success: parse response and populate all fields

4. **Risk scoring logic:**
   - `whetherCompanyDefaulter` == "Yes" → `is_defaulter = TRUE`, risk flag "DEFAULTER"
   - `whetherVanishingCompanyYN` == "Yes" → `is_vanishing = TRUE`, risk flag "VANISHING"
   - `whetherCompanyDormantCompany` == "Yes" → `is_dormant = TRUE`, risk flag "DORMANT"
   - `companyStatus` != "Active" → risk flag "INACTIVE"
   - Risk level: HIGH if any of (DEFAULTER, VANISHING), MEDIUM if any of (DORMANT, INACTIVE), LOW otherwise

5. **Resumable:** The script must be safe to stop and restart. It processes only CINs not yet in `mca_company_enrichment` or those with `api_status = 'FAILED'`. Each CIN is committed individually (no batch commit that loses progress on crash).

6. **Progress logging:**
   - Log every 10 CINs: `[10/500] Processed CIN U72200MH2010PTC123456 — Active, no flags`
   - Log every HIGH risk immediately: `🚨 HIGH RISK: U72200MH2010PTC123456 — DEFAULTER, VANISHING`
   - At the end: summary with total processed, success, failed, high risk, medium risk, low risk counts
   - Log to both stdout and `logs/mca_enrichment.log`

7. **CLI interface:**
   - `python scripts/mca_enrichment.py` — full run (extract CINs, enrich all pending)
   - `python scripts/mca_enrichment.py --limit 50` — process only first 50 unprocessed CINs (for testing)
   - `python scripts/mca_enrichment.py --retry-failed` — re-process only FAILED CINs
   - `python scripts/mca_enrichment.py --stats` — print summary stats without processing anything
   - `python scripts/mca_enrichment.py --dry-run` — extract CINs and show count, but don't call API

### Step 2D: Test it

1. Run `--dry-run` first to see how many CINs are found and from which columns
2. Run `--limit 10` to process 10 CINs and verify the API calls work
3. Show the results: `SELECT cin, company_name, risk_level, risk_flags, api_status FROM mca_company_enrichment ORDER BY risk_level DESC LIMIT 20;`
4. Show the stats: `python scripts/mca_enrichment.py --stats`
5. If it's working, run `--limit 50` to do a bigger batch and verify rate limiting is working (should take ~50 seconds)

### Step 2E: Do NOT run a full enrichment yet

The full run could be thousands of CINs at 1/sec — that's hours. Just verify it works on 50. I'll start the full run in a tmux session later.

---

## FINAL CHECKLIST (after both tasks)

1. Confirm both scripts are executable: `chmod +x scripts/monitor_sources.py scripts/mca_enrichment.py`
2. Show me the contents of `logs/` directory
3. Show me `source_health` table row count
4. Show me `mca_company_enrichment` table row count and a few sample rows
5. Run `git add -A && git status` — show what's staged (do NOT commit yet, I'll review first)
6. Print a final summary of what was built, what works, and any issues encountered