AML WATCHLIST PIPELINE — DATABASE DEMO QUERIES
================================================
This file contains ready-to-run PostgreSQL queries for
demonstrating the AML watchlist screening pipeline.

The DEMO FLOW section is designed to be run top-to-bottom
during a CTO/stakeholder demo — each query builds on the
previous one to tell a story:
  Scale → Breadth → Screening → Intelligence → Quality

CONNECT:
  psql -U aayush -d risk_pipeline
  Password: aayush123

================================================
DEMO FLOW (run in this order for CTO)
================================================

-- 1. HEADLINE: Total scale
-- Shows the three big numbers: total records, unique agencies,
-- and unique source lists. This is your opening slide —
-- "we built a database of 105K+ records across 63 agencies."

SELECT count(*) AS total_records,
       count(DISTINCT source_agency) AS agencies,
       count(DISTINCT source_list) AS source_lists  
FROM watchlist_records;

-- 2. BREADTH: Records by agency (top 20)
-- Shows which agencies contribute the most data.
-- SEBI dominates with 44K+, followed by NSE, MHA, BSE.
-- Proves we're not just scraping one source — this is
-- a comprehensive multi-agency pipeline.

SELECT source_agency, count(*) AS records
FROM watchlist_records
GROUP BY source_agency
ORDER BY records DESC
LIMIT 20;

-- 3. LIVE SCREENING: Search any name across all watchlists
-- THIS IS THE PRODUCT. Type any Indian name and instantly
-- see if they appear on any government watchlist.
-- Ask the CTO to suggest a name live — makes it interactive.
-- Try common surnames: sharma, kumar, gupta, singh, patel

SELECT source_agency, source_list, name, details
FROM watchlist_records
WHERE name ILIKE '%sharma%'
ORDER BY source_agency
LIMIT 20;

-- 4. CROSS-AGENCY HITS: Entities flagged by multiple agencies
-- The highest-value intelligence in the whole database.
-- If someone appears on SEBI + BSE + NSE + NSDL, that's a
-- much bigger red flag than a single listing. This is what
-- AML compliance teams pay premium for — cross-referencing.
-- Example: "Sai Prakash Organic Food Limited" appears across
-- BSE + NSE + NSDL + SEBI = high risk entity.

SELECT name,
       count(DISTINCT source_agency) AS agency_hits,
       array_agg(DISTINCT source_agency) AS agencies
FROM watchlist_records
WHERE name IS NOT NULL AND trim(name) != ''
GROUP BY name
HAVING count(DISTINCT source_agency) > 1
ORDER BY agency_hits DESC
LIMIT 15;

-- 5. DATA QUALITY: Field completeness
-- Shows how clean our data is. 99.9% of records have names,
-- 99.3% have details, 77% have document URLs linking back
-- to the original source. This proves the pipeline isn't
-- just scraping garbage — it's structured, validated data.

SELECT count(*) AS total,
       count(name) AS has_name,
       count(details) AS has_details,
       count(document_url) AS has_doc_url,
       count(detail_page_url) AS has_detail_url
FROM watchlist_records;

-- 6. SPECIFIC AGENCY DEEP-DIVE: SEBI enforcement
-- Zooms into our biggest source (44K+ records) and shows
-- the breakdown by sub-list: Orders of AO, Recovery
-- Proceedings, Chairperson Orders, etc. Demonstrates
-- we're not just hitting one endpoint — we mapped SEBI's
-- entire enforcement data structure via their internal API.

SELECT source_list, count(*) AS records
FROM watchlist_records
WHERE source_agency = 'SEBI'
GROUP BY source_list
ORDER BY records DESC;

-- 7. MOST WANTED / HIGH RISK: Law enforcement lists
-- The dramatic closer. Shows actual CBI most wanted,
-- NIA terrorists, UP Police wanted persons, and MHA
-- banned organizations. This is the data that banks
-- are legally required to screen against (PMLA/UAPA).
-- Reward amounts show up here for wanted persons.

SELECT source_agency, source_list, name, reward_amount, address
FROM watchlist_records
WHERE source_agency IN ('CBI', 'NIA', 'UP Police', 'MHA')
ORDER BY source_agency, source_list
LIMIT 30;

================================================
QUICK REFERENCE (day-to-day use)
================================================

-- See first 5 rows (quick sanity check on schema/data shape)
SELECT * FROM watchlist_records LIMIT 5;

-- Count records for a specific agency
SELECT count(*) FROM watchlist_records WHERE source_agency = 'SEBI';

-- Search by name (basic lookup, useful for spot-checking)
SELECT name, source_agency, address
FROM watchlist_records
WHERE name ILIKE '%kumar%'
LIMIT 10;

-- Exit psql
\q