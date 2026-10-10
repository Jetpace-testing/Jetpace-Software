-- 0004_consumption_period_start.sql
--
-- consumption() measured staleness from the caller's raw `p_from`, but it
-- reads reading_daily, which is bucketed by DAY in the site's timezone.
-- Those two things only agree when p_from happens to land on midnight.
--
-- Called with `now() - interval '7 days'` - an ordinary thing to ask - the
-- window's first day bucket opens up to 24 hours after p_from, which blew
-- straight past the 90-minute gap allowance and reported `stale_open` for
-- meters that had been recording continuously for a month.
--
-- The seeded demo data showed this immediately. It would have shown up at
-- a customer as "why is last week's consumption blank".
--
-- The fix is to measure from the start of the period as the ROLLUP sees
-- it. `p_from` is snapped down to its day boundary and `p_to` up to the
-- next one, so a request for "the last seven days" means seven whole days
-- in the site's own time - which is what anybody asking that question
-- means, and what a billing period is anyway.
--
-- Note this is NOT guessing at a figure the system cannot defend. The
-- boundary is being read at the granularity the data is stored at; the
-- staleness test itself is unchanged, and still refuses when a meter
-- genuinely was not recording across the period start.
--
-- Known limitation, deliberately left: the timezone is a parameter
-- defaulting to Asia/Kolkata rather than being read from site.timezone,
-- because reading_daily's own bucket is fixed at creation time in 0002.
-- Making the rollup timezone per-site needs one aggregate per zone, and
-- that decision belongs with multi-site support.

BEGIN;

DROP FUNCTION IF EXISTS consumption(bigint, smallint, timestamptz, timestamptz, interval);

CREATE FUNCTION consumption(
    p_device  bigint,
    p_param   smallint,
    p_from    timestamptz,
    p_to      timestamptz,
    p_max_gap interval DEFAULT INTERVAL '90 minutes',
    p_tz      text     DEFAULT 'Asia/Kolkata'
) RETURNS TABLE (
    open_value   double precision,
    close_value  double precision,
    used         double precision,
    status       text,
    period_start timestamptz,
    period_end   timestamptz
) LANGUAGE sql STABLE AS $$
    WITH bounds AS (
        -- the period as the daily rollup sees it
        SELECT time_bucket(INTERVAL '1 day', p_from, p_tz) AS pstart,
               CASE
                   WHEN p_to = time_bucket(INTERVAL '1 day', p_to, p_tz)
                       THEN p_to
                   ELSE time_bucket(INTERVAL '1 day', p_to, p_tz)
                        + INTERVAL '1 day'
               END AS pend
    ),
    param AS (
        SELECT aggregation FROM parameter WHERE id = p_param
    ),
    b AS (
        SELECT first(open_value, day)  AS ov,
               last (close_value, day) AS cv,
               min(open_ts)            AS ots,
               sum(samples)            AS n
        FROM   reading_daily, bounds
        WHERE  device_id = p_device AND param = p_param
          AND  day >= bounds.pstart AND day < bounds.pend
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
            WHEN param.aggregation <> 'counter'          THEN NULL
            WHEN b.n IS NULL OR b.n = 0                  THEN NULL
            WHEN b.cv < b.ov                             THEN NULL
            WHEN b.ots - bounds.pstart > p_max_gap
             AND hist.first_ever < bounds.pstart         THEN NULL
            ELSE b.cv - b.ov
        END,
        CASE
            WHEN param.aggregation IS NULL               THEN 'no_such_parameter'
            WHEN param.aggregation <> 'counter'          THEN 'not_a_counter'
            WHEN b.n IS NULL OR b.n = 0                  THEN 'no_data'
            WHEN b.cv < b.ov                             THEN 'counter_reset'
            WHEN b.ots - bounds.pstart > p_max_gap
             AND hist.first_ever < bounds.pstart         THEN 'stale_open'
            WHEN hist.first_ever >= bounds.pstart        THEN 'partial_new_meter'
            ELSE 'ok'
        END,
        bounds.pstart,
        bounds.pend
    FROM b CROSS JOIN hist CROSS JOIN bounds LEFT JOIN param ON true;
$$;

INSERT INTO schema_migrations (version, name)
VALUES (4, '0004_consumption_period_start')
ON CONFLICT (version) DO NOTHING;

COMMIT;
