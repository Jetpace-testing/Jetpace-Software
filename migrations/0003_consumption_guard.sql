-- 0003_consumption_guard.sql
--
-- consumption() answers a question that only makes sense for a counter:
-- "how much accumulated between these two moments". Applied to an
-- instantaneous parameter it returns an arithmetic difference that means
-- nothing - running it against a differential pressure gave
-- "used = 1.0 Pa", which is not a quantity of anything.
--
-- The prototype produced exactly that, and it would have reached a report
-- eventually. So the function now refuses rather than answering: a
-- parameter is either declared `counter` or it is not billable, and the
-- status says which.
--
-- This is the same principle as the rest of the billing engine. A figure
-- the system cannot defend is withheld with a reason attached, never
-- replaced by a plausible-looking number.

BEGIN;

DROP FUNCTION IF EXISTS consumption(bigint, smallint, timestamptz, timestamptz, interval);

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
    WITH param AS (
        SELECT aggregation FROM parameter WHERE id = p_param
    ),
    b AS (
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
            -- an instantaneous value never accumulates, so there is no
            -- consumption to report. The counters are still returned, so
            -- a trend view can use them.
            WHEN param.aggregation <> 'counter'      THEN NULL
            WHEN b.n IS NULL OR b.n = 0              THEN NULL
            WHEN b.cv < b.ov                         THEN NULL
            WHEN b.ots - p_from > p_max_gap
             AND hist.first_ever < p_from            THEN NULL
            ELSE b.cv - b.ov
        END,
        CASE
            WHEN param.aggregation IS NULL           THEN 'no_such_parameter'
            WHEN param.aggregation <> 'counter'      THEN 'not_a_counter'
            WHEN b.n IS NULL OR b.n = 0              THEN 'no_data'
            WHEN b.cv < b.ov                         THEN 'counter_reset'
            WHEN b.ots - p_from > p_max_gap
             AND hist.first_ever < p_from            THEN 'stale_open'
            WHEN hist.first_ever >= p_from           THEN 'partial_new_meter'
            ELSE 'ok'
        END
    FROM b CROSS JOIN hist LEFT JOIN param ON true;
$$;

INSERT INTO schema_migrations (version, name) VALUES (3, '0003_consumption_guard')
ON CONFLICT (version) DO NOTHING;

COMMIT;
