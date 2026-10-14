"""
Every query the environmental product runs.

Moved out of jetpace_core.web.queries, where all three of these sat
alongside the shell's own. They are here because all three contain the
literal `p.key = 'dp'`: a shared query that knows the word "dp" is the
shared layer knowing about one product, and a customer who bought the
energy product was having rooms and excursions computed for every page
they loaded.

The connection comes from core - `queries.connect` - so exactly one piece
of code decides the row factory and the connect timeout. All of this
reads; the dashboard has no write path, which is why the app role's
append-only grant on `reading` costs it nothing.
"""

from __future__ import annotations

import psycopg

# The cleanroom figures, which used to sit in the shared SUMMARY and so
# were computed for every page including the energy one.
ENVIRONMENT_SUMMARY = """
WITH dp AS (
    SELECT DISTINCT ON (r.device_id)
           r.device_id, r.value, r.ts
    FROM   reading r
    JOIN   device d    ON d.id = r.device_id AND d.retired_at IS NULL
    JOIN   parameter p ON p.id = r.param AND p.key = 'dp'
    ORDER  BY r.device_id, r.ts DESC
)
SELECT
    (SELECT count(*) FROM dp)                                     AS rooms_total,
    (SELECT count(*) FROM dp WHERE value > 0)                     AS rooms_in_spec,
    -- The average of the CURRENT reading in each room, not of every
    -- reading taken: the question is what the cascade is doing now, and
    -- averaging a day of history would bury an excursion that started an
    -- hour ago.
    (SELECT round(avg(value)::numeric, 1) FROM dp)                AS avg_dp,
    -- Each raw reading stands for one minute, so counting rows IS a
    -- duration. True only while every pressure point shares a 1-minute
    -- scan class; see the note on EXCURSIONS below.
    (SELECT count(*) FROM reading r
       JOIN parameter p ON p.id = r.param AND p.key = 'dp'
      WHERE r.ts > now() - interval '24 hours' AND r.value <= 0)   AS excursions_24h
"""


# ---------------------------------------------------------------------------
# pressure trend - from the hourly rollup, not from raw
#
# 24 hours of raw data for three rooms is 4,320 rows; the same window from
# reading_hourly is 72. The chart cannot tell the difference and the
# database very much can.
# ---------------------------------------------------------------------------

DP_TREND = """
SELECT d.code,
       d.name,
       h.bucket,
       round(h.avg_value::numeric, 1) AS avg,
       round(h.min_value::numeric, 1) AS min,
       round(h.max_value::numeric, 1) AS max
FROM   reading_hourly h
JOIN   device d    ON d.id = h.device_id AND d.retired_at IS NULL
JOIN   parameter p ON p.id = h.param AND p.key = 'dp'
WHERE  h.bucket > now() - %(hours)s * interval '1 hour'
ORDER  BY d.code, h.bucket
"""


# ---------------------------------------------------------------------------
# excursions
#
# Each raw reading stands for one minute, so counting rows IS a duration.
# That holds only while every pressure point is on a 1-minute scan class;
# when scan classes diverge this must become a real time-weighted
# calculation. Stated here because it is load-bearing and invisible.
# ---------------------------------------------------------------------------

EXCURSIONS = """
SELECT d.code,
       d.name,
       count(*) FILTER (WHERE r.value <= 0)                   AS minutes_below_zero,
       count(*)                                               AS minutes_total,
       round(min(r.value)::numeric, 1)                        AS worst,
       count(*) FILTER (WHERE r.value <= 0) = 0               AS clean
FROM   reading r
JOIN   device d    ON d.id = r.device_id AND d.retired_at IS NULL
JOIN   parameter p ON p.id = r.param AND p.key = 'dp'
WHERE  r.ts > now() - %(days)s * interval '1 day'
GROUP  BY d.code, d.name
ORDER  BY minutes_below_zero DESC, d.code
"""


# ---------------------------------------------------------------------------
# running them
# ---------------------------------------------------------------------------

def environment_summary(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(ENVIRONMENT_SUMMARY)
        return cur.fetchone() or {}


def dp_trend(conn: psycopg.Connection, hours: int = 24) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(DP_TREND, {"hours": hours})
        return cur.fetchall()


def excursions(conn: psycopg.Connection, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(EXCURSIONS, {"days": days})
        return cur.fetchall()


# ---------------------------------------------------------------------------
# excursion EPISODES, as distinct from excursion minutes
#
# EXCURSIONS above answers "how much of the last week was this room out of
# spec", which is the figure a quality manager signs. This answers "when
# did it happen", which is the figure an operator acts on - and they are
# not the same question. Forty-one minutes below zero could be one door
# held open or forty-one separate one-minute dips, and only the second is
# a cascade that is failing.
#
# So consecutive samples below zero are collapsed into one episode: an
# island of `below` rows between two rows that were in spec. The running
# count of in-spec rows is what numbers the islands - a standard
# gaps-and-islands shape, and the only part of this query that is not
# obvious on sight.
#
# ONE ROW STANDS FOR ONE MINUTE, which is the same assumption EXCURSIONS
# makes and is load-bearing in the same way: it holds only while every
# pressure point is on a 1-minute scan class. When scan classes diverge
# this has to become a real time difference between the island's ends.
#
# `ongoing` is whether the episode's last sample is also the room's last
# sample. A room that is still reversed needs somebody now; a room that
# recovered an hour ago is history, and the panel draws them differently.
# ---------------------------------------------------------------------------

EXCURSION_EPISODES = """
WITH samples AS (
    SELECT d.code, d.name, r.ts, r.value, (r.value <= 0) AS below
    FROM   reading r
    JOIN   device d    ON d.id = r.device_id AND d.retired_at IS NULL
    JOIN   parameter p ON p.id = r.param AND p.key = 'dp'
    WHERE  r.ts > now() - %(hours)s * interval '1 hour'
),
newest AS (
    SELECT code, max(ts) AS ts FROM samples GROUP BY code
),
marked AS (
    SELECT s.*,
           count(*) FILTER (WHERE NOT s.below) OVER (
               PARTITION BY s.code ORDER BY s.ts
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           ) AS island
    FROM   samples s
)
SELECT m.code,
       m.name,
       min(m.ts)                        AS started,
       max(m.ts)                        AS ended,
       count(*)                         AS minutes,
       round(min(m.value)::numeric, 1)  AS worst,
       max(m.ts) = n.ts                 AS ongoing
FROM   marked m
JOIN   newest n ON n.code = m.code
WHERE  m.below
GROUP  BY m.code, m.name, m.island, n.ts
ORDER  BY min(m.ts) DESC
LIMIT  %(limit)s
"""


def excursion_episodes(conn: psycopg.Connection, hours: int = 24,
                       limit: int = 8) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(EXCURSION_EPISODES, {"hours": hours, "limit": limit})
        return cur.fetchall()
