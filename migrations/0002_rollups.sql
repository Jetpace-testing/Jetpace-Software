-- 0002_rollups.sql
--
-- The rollups, and the consumption function that reads them.
--
-- This is where TimescaleDB earns its place. Billing reduces a period to
-- its FIRST reading and its LAST reading, and those are exactly what a
-- continuous aggregate materialises incrementally. A monthly report over
-- 400 devices stops being a scan of millions of raw rows and becomes a
-- lookup of thirty per series.
--
-- Note the aggregate carries both kinds of summary. A counter needs
-- first/last, because consumption is last - first and an average kWh
-- reading is meaningless. An instantaneous value needs min/max/avg,
-- because you want the range. Rather than two tables, one table carries
-- both and each parameter reads the columns that mean something for it.

BEGIN;

-- ---------------------------------------------------------------------------
-- hourly, from raw
-- ---------------------------------------------------------------------------

CREATE MATERIALIZED VIEW reading_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 hour', ts) AS bucket,
    device_id,
    param,
    first(value, ts) AS open_value,
    last (value, ts) AS close_value,
    min(ts)          AS open_ts,
    max(ts)          AS close_ts,
    count(*)         AS samples,
    avg(value)       AS avg_value,
    max(value)       AS max_value,
    min(value)       AS min_value
FROM reading
WHERE quality = 0          -- substituted and suspect samples are excluded
GROUP BY bucket, device_id, param
WITH NO DATA;

-- start_offset covers late arrivals from a store-and-forward replay;
-- end_offset keeps the aggregate off the bucket still being written.
SELECT add_continuous_aggregate_policy('reading_hourly',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes');

-- ---------------------------------------------------------------------------
-- daily, from hourly - not from raw
--
-- Built on the hourly aggregate so a month's report touches 30 rows per
-- series and the compressed raw chunks are never scanned again.
--
-- Bucketed in the site's own timezone. "A month is measured from its own
-- midnight" is only true if the bucket knows the zone, and that arithmetic
-- must never be done in application code.
-- ---------------------------------------------------------------------------

CREATE MATERIALIZED VIEW reading_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 day', bucket, 'Asia/Kolkata') AS day,
    device_id,
    param,
    first(open_value,  bucket) AS open_value,
    last (close_value, bucket) AS close_value,
    min(open_ts)               AS open_ts,
    max(close_ts)              AS close_ts,
    sum(samples)               AS samples,
    max(max_value)             AS max_value,
    min(min_value)             AS min_value
FROM reading_hourly
GROUP BY day, device_id, param
WITH NO DATA;

SELECT add_continuous_aggregate_policy('reading_daily',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes');

-- ---------------------------------------------------------------------------
-- consumption
--
-- The billing rules from Meril EMS, expressed once. The status column is
-- the important part: it is what a report renders instead of a silent
-- blank, and a blank that explains itself is the behaviour that makes the
-- numbers trustworthy.
--
--   ok                 measured end to end
--   partial_new_meter  commissioned mid-period; bills what it recorded
--   counter_reset      register rolled over or device replaced; NOT a
--                      negative bill
--   stale_open         not recording across the period start; both
--                      counters are still shown so the gap is visible
--   no_data            nothing recorded at all
-- ---------------------------------------------------------------------------

CREATE FUNCTION consumption(
    p_device  bigint,
    p_param   smallint,
    p_from    timestamptz,
    p_to      timestamptz,
    p_max_gap interval DEFAULT INTERVAL '90 minutes'
) RETURNS TABLE (
    open_value  double precision,
    close_value double precision,
    used        double precision,
    status      text
) LANGUAGE sql STABLE AS $$
    WITH b AS (
        SELECT first(open_value, day)  AS ov,
               last (close_value, day) AS cv,
               min(open_ts)            AS ots,
               sum(samples)            AS n
        FROM   reading_daily
        WHERE  device_id = p_device AND param = p_param
          AND  day >= p_from AND day < p_to
    ),
    hist AS (
        SELECT min(open_ts) AS first_ever
        FROM   reading_daily
        WHERE  device_id = p_device AND param = p_param
    )
    SELECT
        b.ov,
        b.cv,
        CASE
            WHEN b.n IS NULL OR b.n = 0              THEN NULL
            WHEN b.cv < b.ov                         THEN NULL
            WHEN b.ots - p_from > p_max_gap
             AND hist.first_ever < p_from            THEN NULL
            ELSE b.cv - b.ov
        END,
        CASE
            WHEN b.n IS NULL OR b.n = 0              THEN 'no_data'
            WHEN b.cv < b.ov                         THEN 'counter_reset'
            WHEN b.ots - p_from > p_max_gap
             AND hist.first_ever < p_from            THEN 'stale_open'
            WHEN hist.first_ever >= p_from           THEN 'partial_new_meter'
            ELSE 'ok'
        END
    FROM b CROSS JOIN hist;
$$;

INSERT INTO schema_migrations (version, name) VALUES (2, '0002_rollups')
ON CONFLICT (version) DO NOTHING;

COMMIT;
