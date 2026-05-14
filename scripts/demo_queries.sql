-- AML Pipeline — Demo queries (CTO-ready)
-- =============================================================
-- Database : risk_pipeline (PostgreSQL 16)
-- Table    : watchlist_records  (project schema names this table
--            watchlist_records; if you prefer the shorter alias
--            `watchlist`, create a view:
--              CREATE VIEW watchlist AS SELECT * FROM watchlist_records;)
-- Connect  : psql -h localhost -U aayush -d risk_pipeline
-- =============================================================


-- ---------------------------------------------------------------
-- 1. SCREENING QUERY — search a name across every watchlist
-- ---------------------------------------------------------------
--    Replace 'sharma' with the candidate name. ILIKE is
--    case-insensitive; % wildcards match anywhere in the value.
SELECT source_agency,
       source_list,
       name,
       details,
       scraped_at
FROM   watchlist_records
WHERE  name ILIKE '%sharma%'
ORDER  BY source_agency
LIMIT  50;


-- ---------------------------------------------------------------
-- 2. SEBI ENFORCEMENT — every SEBI order (newest first)
-- ---------------------------------------------------------------
SELECT source_list,
       name,
       details,
       document_url,
       scraped_at
FROM   watchlist_records
WHERE  source_agency = 'SEBI'
ORDER  BY source_list, scraped_at DESC
LIMIT  50;


-- ---------------------------------------------------------------
-- 3. BANNED ORGANIZATIONS — MHA banned orgs + UAPA terrorists
-- ---------------------------------------------------------------
SELECT source_list,
       name,
       details,
       scraped_at
FROM   watchlist_records
WHERE  source_agency = 'MHA'
ORDER  BY source_list;


-- ---------------------------------------------------------------
-- 4. MOST WANTED — CBI + NIA + UP Police
-- ---------------------------------------------------------------
SELECT source_agency,
       source_list,
       name,
       address,
       reward_amount,
       details
FROM   watchlist_records
WHERE  source_agency IN ('CBI', 'NIA', 'UP Police')
ORDER  BY source_agency, source_list;


-- ---------------------------------------------------------------
-- 5. WILFUL DEFAULTERS — bank defaulter lists
-- ---------------------------------------------------------------
SELECT source_agency,
       name,
       details,
       scraped_at
FROM   watchlist_records
WHERE  source_list ILIKE '%defaulter%'
   OR  source_list ILIKE '%wilful%'
ORDER  BY source_agency;


-- ---------------------------------------------------------------
-- 6. CROSS-AGENCY HIT — names appearing in 2+ agencies (high risk)
-- ---------------------------------------------------------------
--    A name surfacing on, say, CBI + NIA + SEBI is a stronger
--    signal than appearing on any one list alone.
SELECT name,
       count(DISTINCT source_agency)             AS agency_hits,
       array_agg(DISTINCT source_agency)         AS agencies
FROM   watchlist_records
WHERE  name IS NOT NULL
  AND  trim(name) <> ''
GROUP  BY name
HAVING count(DISTINCT source_agency) > 1
ORDER  BY agency_hits DESC
LIMIT  30;


-- ---------------------------------------------------------------
-- 7. COVERAGE DASHBOARD — one-row pipeline summary
-- ---------------------------------------------------------------
SELECT count(*)                                                AS total_records,
       count(DISTINCT source_agency)                            AS agencies,
       count(DISTINCT source_list)                              AS source_lists,
       count(*) FILTER (WHERE document_url    IS NOT NULL
                          AND trim(document_url)    <> '')      AS with_documents,
       count(*) FILTER (WHERE detail_page_url IS NOT NULL
                          AND trim(detail_page_url) <> '')      AS with_detail_pages
FROM   watchlist_records;


-- ---------------------------------------------------------------
-- 8. BONUS — fuzzy name screening with trigram similarity
-- ---------------------------------------------------------------
--    One-time setup:  CREATE EXTENSION IF NOT EXISTS pg_trgm;
--    Then replace 'Vijay Mallya' with the candidate.
-- SELECT source_agency,
--        source_list,
--        name,
--        similarity(lower(name), lower('Vijay Mallya')) AS sim
-- FROM   watchlist_records
-- WHERE  name % 'Vijay Mallya'             -- trigram similarity > 0.3
-- ORDER  BY sim DESC
-- LIMIT  20;
