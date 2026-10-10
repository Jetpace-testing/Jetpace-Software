"""
Every query the dashboard runs, in one file.

SQL lives here rather than scattered through request handlers, for three
reasons: it can be read as a set and reviewed as a set, it can be run by
hand in psql when something looks wrong, and the handlers stay about HTTP.

All of it reads. The dashboard has no write path at all, which is why the
app role's append-only grant on `reading` costs it nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

# A device that has not reported for longer than this is shown as offline.
# Deliberately generous: the prototype's scan interval is 2 s, but a device
# in offline backoff is skipped for several passes, so a tight threshold
# would flap. Stage 2b replaces this entirely with a retained MQTT status
# topic, at which point "offline" stops being a guess about silence.
OFFLINE_AFTER = "interval '90 seconds'"


@dataclass(frozen=True)
class Summary:
    devices: int
    online: int
    rooms_in_spec: int
    rooms_total: int
    excursions_24h: int
    readings_24h: int
    newest: datetime | None


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


# ---------------------------------------------------------------------------
# summary - the figures that go above the detail
# ---------------------------------------------------------------------------

SUMMARY = f"""
WITH latest AS (
    SELECT DISTINCT ON (r.device_id, r.param)
           r.device_id, r.param, r.ts, r.value
    FROM   reading r
    JOIN   device d ON d.id = r.device_id AND d.retired_at IS NULL
    ORDER  BY r.device_id, r.param, r.ts DESC
),
dp AS (
    SELECT l.device_id, l.value, l.ts
    FROM   latest l
    JOIN   parameter p ON p.id = l.param AND p.key = 'dp'
)
SELECT
    (SELECT count(*) FROM device WHERE retired_at IS NULL)              AS devices,
    (SELECT count(*) FROM latest
      WHERE ts > now() - {OFFLINE_AFTER})                               AS online_points,
    (SELECT count(DISTINCT device_id) FROM latest
      WHERE ts > now() - {OFFLINE_AFTER})                               AS online,
    (SELECT count(*) FROM dp)                                           AS rooms_total,
    (SELECT count(*) FROM dp WHERE value > 0)                           AS rooms_in_spec,
    (SELECT count(*) FROM reading r
       JOIN parameter p ON p.id = r.param AND p.key = 'dp'
      WHERE r.ts > now() - interval '24 hours' AND r.value <= 0)        AS excursions_24h,
    (SELECT count(*) FROM reading
      WHERE ts > now() - interval '24 hours')                           AS readings_24h,
    (SELECT max(ts) FROM reading)                                       AS newest
"""


# ---------------------------------------------------------------------------
# the live table
# ---------------------------------------------------------------------------

LIVE = f"""
SELECT DISTINCT ON (d.id, p.key)
       d.code,
       d.name,
       g.label                                   AS gateway,
       d.unit_id,
       p.key                                     AS param,
       p.unit,
       p.decimals,
       p.aggregation,
       r.value,
       r.ts,
       r.ts > now() - {OFFLINE_AFTER}            AS online,
       -- GREATEST(0, ...) because TWO clocks are involved: the poller
       -- timestamps the reading in Python, now() is the database's
       -- transaction start, and a fraction of a second of skew between
       -- them renders as "-1 s". Harmless here; across a WAN to an edge
       -- agent it would not be, which is why §14 insists on NTP.
       GREATEST(0, EXTRACT(EPOCH FROM (now() - r.ts)))::int AS age_s
FROM   device d
JOIN   gateway g      ON g.id = d.gateway_id
JOIN   reading r      ON r.device_id = d.id
JOIN   parameter p    ON p.id = r.param
WHERE  d.retired_at IS NULL
ORDER  BY d.id, p.key, r.ts DESC
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
# energy - through consumption(), so the statuses are the real ones
# ---------------------------------------------------------------------------

ENERGY_DAILY = """
SELECT d.code,
       d.name,
       day::date                              AS day,
       round((close_value - open_value)::numeric, 1) AS used
FROM   reading_daily h
JOIN   device d    ON d.id = h.device_id AND d.retired_at IS NULL
JOIN   parameter p ON p.id = h.param AND p.key = 'kwh_import'
WHERE  day > now() - %(days)s * interval '1 day'
  AND  close_value >= open_value          -- a reset is not a day's consumption
ORDER  BY d.code, day
"""

ENERGY_PERIOD = """
SELECT d.code,
       d.name,
       round(c.used::numeric, 1)   AS used,
       c.status,
       c.period_start::date        AS period_start,
       c.period_end::date          AS period_end
FROM   device d
JOIN   parameter p ON p.key = 'kwh_import'
CROSS  JOIN LATERAL consumption(d.id, p.id,
             now() - %(days)s * interval '1 day', now()) c
WHERE  d.retired_at IS NULL
  AND  EXISTS (SELECT 1 FROM reading_daily rd
                WHERE rd.device_id = d.id AND rd.param = p.id)
ORDER  BY d.code
"""


# ---------------------------------------------------------------------------

def summary(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(SUMMARY)
        return cur.fetchone() or {}


def live(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(LIVE)
        return cur.fetchall()


def dp_trend(conn: psycopg.Connection, hours: int = 24) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(DP_TREND, {"hours": hours})
        return cur.fetchall()


def excursions(conn: psycopg.Connection, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(EXCURSIONS, {"days": days})
        return cur.fetchall()


def energy_daily(conn: psycopg.Connection, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(ENERGY_DAILY, {"days": days})
        return cur.fetchall()


def energy_period(conn: psycopg.Connection, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(ENERGY_PERIOD, {"days": days})
        return cur.fetchall()
