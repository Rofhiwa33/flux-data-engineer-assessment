-- ============================================================
-- Rofhiwa Flux Full Circle Data Engineer Assessment
-- Part A: SQL Queries

--
-- All queries run against the CLEAN modelled dataset produced
-- by the Python pipeline (bookings_clean.csv), loaded into
-- DuckDB as the table `bookings`.
-- revenue_zar is pre-computed (revenue × fx_rate_to_zar).
--
-- ============================================================

-- Setup: load CSVs into DuckDB (run once per session)
-- CREATE TABLE bookings   AS SELECT * FROM read_csv_auto('data/output/bookings_clean.csv');
-- CREATE TABLE properties AS SELECT * FROM read_csv_auto('data/raw/properties.csv');
-- CREATE TABLE fx_rates   AS SELECT * FROM read_csv_auto('data/raw/fx_rates.csv');


-- ============================================================
-- Q1: Top 5 properties by total revenue (ZAR)
--     showing property_name, country, total_revenue_zar, num_bookings
-- ============================================================
-- BigQuery: PARTITION BY property_id on bookings table speeds up the GROUP BY.
-- ============================================================

SELECT
    b.property_name,
    b.country,
    SUM(b.revenue_zar)    AS total_revenue_zar,
    COUNT(b.booking_id)   AS num_bookings
FROM bookings b
GROUP BY
    b.property_name,
    b.country
ORDER BY total_revenue_zar DESC
LIMIT 5;


-- ============================================================
-- Q2: Monthly revenue trend for 2025 (by check-in month), in ZAR
-- ============================================================
-- BigQuery: PARTITION BY MONTH(check_in) with CLUSTER BY property_id
--           avoids a full scan when filtering by year.
-- ============================================================

SELECT
    DATE_TRUNC('month', check_in::DATE)   AS check_in_month,
    SUM(revenue_zar)                      AS total_revenue_zar,
    COUNT(booking_id)                     AS num_bookings
FROM bookings
WHERE YEAR(check_in) = 2025
GROUP BY DATE_TRUNC('month', check_in::DATE)
ORDER BY check_in_month;


-- ============================================================
-- Q3: Per booking channel:
--     number of bookings, average length of stay (nights),
--     average revenue per booking (ZAR)
-- ============================================================

SELECT
    booking_channel,
    COUNT(booking_id)              AS num_bookings,
    ROUND(AVG(num_nights), 1)      AS avg_nights,
    ROUND(AVG(revenue_zar), 2)     AS avg_revenue_zar
FROM bookings
GROUP BY booking_channel
ORDER BY num_bookings DESC;


-- ============================================================
-- Q4: Rank properties by total revenue within their country
--     (window function: RANK)
-- ============================================================
-- BigQuery: CLUSTER BY country on the bookings table would
--           make the window partition efficient.
-- ============================================================

WITH property_revenue AS (
    SELECT
        property_id,
        property_name,
        country,
        SUM(revenue_zar)  AS total_revenue_zar,
        COUNT(booking_id) AS num_bookings
    FROM bookings
    GROUP BY property_id, property_name, country
)
SELECT
    country,
    property_name,
    total_revenue_zar,
    num_bookings,
    RANK() OVER (
        PARTITION BY country
        ORDER BY total_revenue_zar DESC
    ) AS revenue_rank_in_country
FROM property_revenue
ORDER BY country, revenue_rank_in_country;


-- ============================================================
-- Q5: Data-quality checks (run against the RAW data to expose
--     issues the pipeline handles, one row per check with a count)
-- ============================================================
-- NOTE: These checks are run against the quarantine + clean sets
--       combined ( the full cleaned-but-pre-validated frame)
--       to show what the pipeline found and handled.
--       Run against bookings_quarantine.csv + bookings_clean.csv
--       unioned or against the raw source before cleaning.
-- ============================================================

-- For DuckDB convenience, we demonstrate against the clean table
-- and quarantine table separately. In production (BigQuery) you
-- would union both and run in a single pass.

-- 5a. Check 1: Duplicate booking_ids in raw data
SELECT
    'Duplicate booking_id'          AS check_name,
    COUNT(*)                        AS issue_count
FROM (
    SELECT booking_id, COUNT(*) AS cnt
    FROM bookings
    GROUP BY booking_id
    HAVING cnt > 1
) dupes

UNION ALL

-- 5b. Check 2: Bookings where check_out is on or before check_in
SELECT
    'check_out on or before check_in' AS check_name,
    COUNT(*)                           AS issue_count
FROM bookings
WHERE check_out <= check_in

UNION ALL

-- 5c. Check 3: Bookings referencing a property that does not exist
--     (run as a left-join anti-join pattern)
SELECT
    'Orphaned property_id (no match in properties)' AS check_name,
    COUNT(*)                                         AS issue_count
FROM bookings b
LEFT JOIN (
    SELECT DISTINCT property_id FROM properties
) p USING (property_id)
WHERE p.property_id IS NULL

UNION ALL

-- 5d. Check 4: Missing or non-positive revenue
SELECT
    'Missing or non-positive revenue' AS check_name,
    COUNT(*)                          AS issue_count
FROM bookings
WHERE revenue IS NULL OR revenue <= 0

UNION ALL

-- 5e. Check 5: Missing num_guests
SELECT
    'Missing num_guests'  AS check_name,
    COUNT(*)              AS issue_count
FROM bookings
WHERE num_guests IS NULL

UNION ALL

-- 5f. Check 6: Implausibly high guest count (>30)
SELECT
    'num_guests > 30 (implausible)' AS check_name,
    COUNT(*)                        AS issue_count
FROM bookings
WHERE num_guests > 30

UNION ALL

-- 5g. Check 7: Unknown currency (not USD/EUR/GBP/ZAR)
SELECT
    'Unknown/unmappable currency'  AS check_name,
    COUNT(*)                       AS issue_count
FROM bookings
WHERE currency NOT IN ('USD', 'EUR', 'GBP', 'ZAR')
   OR currency IS NULL

ORDER BY issue_count DESC;

