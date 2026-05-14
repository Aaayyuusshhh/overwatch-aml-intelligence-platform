# CONTEXT — Day 3 continuation (same Claude Code session)

Continuing AML data pipeline project. Junior engineer (Aayush).

CURRENT STATE:
- 2 working scrapers: scrapers/cbi_announced_rewards.py, scrapers/cbi_yellow_notices.py
- 2 clean CSVs: data/cbi_announced_rewards.csv (32 rows), data/cbi_yellow_notices.csv (94 rows)
- Schema (16 cols): source_agency, source_list, case_unit, name, father_name, 
  date_of_birth, gender, address, reward_amount, details, has_document, 
  document_url, detail_page_url, interpol_notice_id, link_kind, scraped_at
- Validator: scripts/validate_cbi.py (takes optional CSV path arg)
- Stack: Scrapling 0.4.7, Python 3.12, venv at /home/aayush/risk-pipeline/venv
- NO common/ module, NO base classes, NO premature abstraction

DESIGN DECISIONS MADE:
- Pipeline is a FLAGGING system. Capture name + reference URL per source.
  Enrichment (following links to detail pages) deferred to Phase 2.
- Yellow Notices is COMPLETE. Do not revisit Interpol enrichment.
- Strategy: broad-shallow first (cover many sources), deep-enrich later for
  high-value sources only.

# RULES (unchanged from prior sessions)

1. EXPLAIN BEFORE CODE. 2-4 sentence plan, wait for confirmation.
2. SMALL STEPS. One focused change at a time.
3. NO PREMATURE ABSTRACTION. No common/, no base classes. Duplicate code 
   across scrapers is fine.
4. ASK BEFORE NEW DEPENDENCIES. No silent pip installs.
5. RUN THINGS. Show actual command output, never "this should work."
6. STAY IN SCOPE. Flag out-of-scope issues at end, do not auto-fix.
7. NO NEW DOCS. No README/architecture/workflow files unless asked.
8. NO GIT COMMITS. User controls version control.
9. SCRAPLING ONLY. No requests, beautifulsoup, selenium, playwright, httpx.
10. SHOW VERBATIM OUTPUT. Do not summarize tool output.

# TODAY'S WORK — execute in order, STOP at each gate

## STEP 1: Add enrichment_status column

1a. Add new column `enrichment_status` (17th column) to shared schema.
    Values: 'none', 'partial', 'full'. Default: 'none'.
1b. Update scrapers/cbi_announced_rewards.py and scrapers/cbi_yellow_notices.py
    to write this column with default 'none'.
1c. Backfill data/cbi_announced_rewards.csv and data/cbi_yellow_notices.csv
    with enrichment_status='none' for all existing rows.
1d. Update scripts/validate_cbi.py to recognize the new column.
1e. Run validator against both CSVs. Show output.

STOP. Wait for review.

## STEP 2: Hygiene re-scrape of Announced Rewards

Reason: Last session, the /old-records page returned 0 cards (previously 
returned data). Need to confirm whether this was transient or a permanent 
source-side change.

2a. Re-run scrapers/cbi_announced_rewards.py but write output to a NEW file:
    data/cbi_announced_rewards_recheck.csv. Do NOT overwrite the existing CSV.
2b. Report:
    - Total row count in recheck
    - Whether /old-records returned cards this time (yes/no + count)
    - URLs in recheck NOT in original (new rows)
    - URLs in original NOT in recheck (disappeared rows)
2c. Run validator against recheck CSV.

STOP. Wait for review.

## STEP 3: Recon for CBI scraper #3 — Fugitive Economic Offenders

NO CODE in this step. Recon only.

3a. Find correct URL on cbi.gov.in. Try variants until one returns 200.
    Confirm with a fetch.
3b. Manually examine HTML structure. Report:
    - Layout type (cards / table / paragraphs / other)
    - Total record count visible
    - Whether per-record detail pages exist
    - All available fields per record
    - Any heterogeneity (mixed link types like Yellow Notices had)
    - URL patterns for any external/internal links
3c. Identify closest existing template:
    - Announced Rewards = card grid + per-person detail pages
    - Yellow Notices = single static table, mixed link kinds
    State which is closer and why.
3d. Field-mapping plan: for each of the 17 schema columns, state whether 
    it will be populated for this source and how. Empty columns are 
    expected — list them explicitly.
3e. Flag any schema decisions needed BEFORE writing code. Do not invent 
    new columns without explicit approval.

STOP. Wait for decisions.

## STEP 4: Implementation of scraper #3

Only after Step 3 is reviewed and approved.

4a. Copy the closer-template scraper as starting point. Modify, do not 
    rewrite from scratch.
4b. Reuse cleaning helpers (clean_missing, clean_amount, clean_date) by 
    copying them into the new scraper file. Do NOT extract to common/.
4c. Run scraper. Show output.
4d. Run scripts/validate_cbi.py against the new CSV. Show output.
4e. Report: total row count, link_kind distribution if applicable, any 
    out-of-scope observations.

STOP. Wait for review.

# AFTER STEP 4

Stop. Do not start scraper #4. Wait for next instructions.

# IMPORTANT

- If recon (Step 3) reveals the source requires JavaScript rendering, is a 
  PDF, requires login, or is otherwise structurally incompatible with 
  Fetcher → STOP and report. Do NOT attempt with StealthyFetcher or other 
  tooling without explicit approval. We will pick a different list.
- If validator finds bugs in scraper #3 output, fix in the scraper (not 
  validator) before declaring step done.
- All STOP gates are mandatory. Do not skip even if everything looks fine.