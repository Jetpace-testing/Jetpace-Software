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
)
SELECT
    (SELECT count(*) FROM device WHERE retired_at IS NULL)              AS devices,
    (SELECT count(*) FROM latest
      WHERE ts > now() - {OFFLINE_AFTER})                               AS online_points,
    (SELECT count(DISTINCT device_id) FROM latest
      WHERE ts > now() - {OFFLINE_AFTER})                               AS online,
    (SELECT count(*) FROM reading
      WHERE ts > now() - interval '24 hours')                           AS readings_24h,
    (SELECT max(ts) FROM reading)                                       AS newest
"""

# The cleanroom figures, which used to sit in SUMMARY above and so were
# computed for every page including the energy one. Rooms and excursions
# mean nothing to a customer who bought the energy product, and a shared
# query that knows the word 'dp' is the shared layer knowing about a
# product.
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
    -- Each raw reading stands for one minute, so counting rows IS a
    -- duration. True only while every pressure point shares a 1-minute
    -- scan class; see the note on EXCURSIONS below.
    (SELECT count(*) FROM reading r
       JOIN parameter p ON p.id = r.param AND p.key = 'dp'
      WHERE r.ts > now() - interval '24 hours' AND r.value <= 0)   AS excursions_24h
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
# the device list and one device's detail
#
# Neither mentions a parameter key. dp_trend and energy_daily above are
# tied to 'dp' and 'kwh_import', which is why neither can serve a device
# page - and why those two belong to a product view rather than to the
# shell. These are the shell's own queries: they work for a pressure
# transmitter, an energy meter or a particle counter without being told
# which it is.
# ---------------------------------------------------------------------------

DEVICES = f"""
SELECT d.code,
       d.name,
       g.label                                       AS gateway,
       d.unit_id,
       m.code                                        AS model,
       m.kind,
       count(DISTINCT l.param)                       AS points,
       max(l.ts)                                     AS newest,
       bool_or(l.ts > now() - {OFFLINE_AFTER})       AS online,
       GREATEST(0, EXTRACT(EPOCH FROM (now() - max(l.ts))))::int AS age_s
FROM   device d
JOIN   gateway g      ON g.id = d.gateway_id
JOIN   device_model m ON m.id = d.model_id
LEFT   JOIN LATERAL (
         SELECT DISTINCT ON (r.param) r.param, r.ts
         FROM   reading r
         WHERE  r.device_id = d.id
         ORDER  BY r.param, r.ts DESC
       ) l ON true
WHERE  d.retired_at IS NULL
GROUP  BY d.code, d.name, g.label, d.unit_id, m.code, m.kind
ORDER  BY d.code
"""

DEVICE = """
SELECT d.code,
       d.name,
       d.unit_id,
       d.ct_ratio,
       d.pt_ratio,
       d.commissioned_at,
       g.label         AS gateway,
       g.host,
       g.port,
       m.code          AS model,
       m.revision      AS model_revision,
       m.vendor,
       m.kind,
       m.datasheet_ref,
       s.name          AS site,
       s.timezone
FROM   device d
JOIN   gateway g      ON g.id = d.gateway_id
JOIN   device_model m ON m.id = d.model_id
JOIN   site s         ON s.id = d.site_id
WHERE  d.code = %(code)s AND d.retired_at IS NULL
"""

# What this device actually reports, newest value for each. Driven by the
# data rather than by the model, so a point that has never answered is
# visible by its absence instead of showing a confident blank.
DEVICE_POINTS = f"""
SELECT DISTINCT ON (p.key)
       p.key,
       p.label,
       p.unit,
       p.decimals,
       p.aggregation,
       r.value,
       r.ts,
       r.quality,
       r.ts > now() - {OFFLINE_AFTER}  AS fresh,
       GREATEST(0, EXTRACT(EPOCH FROM (now() - r.ts)))::int AS age_s
FROM   reading r
JOIN   device d    ON d.id = r.device_id AND d.code = %(code)s
JOIN   parameter p ON p.id = r.param
ORDER  BY p.key, r.ts DESC
"""


# ---------------------------------------------------------------------------
# a trend for any device and any parameter
#
# TWO SOURCES, CHOSEN BY WINDOW. Up to a day, raw readings bucketed on the
# way out; beyond that, the hourly rollup bucketed further. The rollup
# cannot serve a short window - an hour of it is one point - and raw cannot
# serve a long one: thirty days for one point at a 2-second scan is 1.3
# million rows to draw 300 pixels.
#
# BUCKET WIDTH IS DERIVED, NOT FIXED. Whatever the window, the result is
# roughly 150-350 points: enough that an excursion is visible, few enough
# that the browser is not asked to draw more marks than the chart has
# pixels. A fixed width gives you three points over an hour or a hundred
# thousand over a month.
#
# min and max travel with avg because an average alone hides the excursion
# that matters - the whole point of looking at a pressure trend.
# ---------------------------------------------------------------------------

TREND_RAW = """
SELECT time_bucket(%(width)s::interval, r.ts) AS bucket,
       avg(r.value)                           AS avg,
       min(r.value)                           AS min,
       max(r.value)                           AS max,
       max(r.quality)                         AS worst_quality,
       count(*)                               AS samples
FROM   reading r
JOIN   device d    ON d.id = r.device_id AND d.code = %(code)s
JOIN   parameter p ON p.id = r.param AND p.key = %(param)s
WHERE  r.ts > now() - %(hours)s * interval '1 hour'
GROUP  BY 1
ORDER  BY 1
"""

TREND_ROLLUP = """
SELECT time_bucket(%(width)s::interval, h.bucket) AS bucket,
       avg(h.avg_value)                           AS avg,
       min(h.min_value)                           AS min,
       max(h.max_value)                           AS max,
       0                                          AS worst_quality,
       sum(h.samples)                             AS samples
FROM   reading_hourly h
JOIN   device d    ON d.id = h.device_id AND d.code = %(code)s
JOIN   parameter p ON p.id = h.param AND p.key = %(param)s
WHERE  h.bucket > now() - %(hours)s * interval '1 hour'
GROUP  BY 1
ORDER  BY 1
"""

# (window in hours, bucket width). The first row whose window is at least
# the requested one wins.
BUCKETS: tuple[tuple[float, str], ...] = (
    (1,     "15 seconds"),
    (6,     "1 minute"),
    (24,    "5 minutes"),
    (72,    "15 minutes"),
    (24 * 7,   "1 hour"),
    (24 * 31,  "6 hours"),
    (24 * 92,  "12 hours"),
)
BUCKET_FALLBACK = "1 day"

# Above this the hourly rollup is used instead of raw readings.
ROLLUP_ABOVE_HOURS = 24


def bucket_width(hours: float) -> str:
    for window, width in BUCKETS:
        if hours <= window:
            return width
    return BUCKET_FALLBACK


# ---------------------------------------------------------------------------

def summary(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(SUMMARY)
        return cur.fetchone() or {}


def environment_summary(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(ENVIRONMENT_SUMMARY)
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


def devices(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(DEVICES)
        return cur.fetchall()


def device(conn: psycopg.Connection, code: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(DEVICE, {"code": code})
        return cur.fetchone()


def device_points(conn: psycopg.Connection, code: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(DEVICE_POINTS, {"code": code})
        return cur.fetchall()


def trend(conn: psycopg.Connection, code: str, param: str,
          hours: float) -> tuple[list[dict], str, str]:
    """
    A trend for one device and one parameter.

    Returns the rows, the bucket width used and which source answered -
    both of those go to the browser so the chart footer can say what it is
    actually showing. "Average over 5 minutes, from the hourly rollup" is
    a different claim from "every reading", and a chart that does not say
    which is inviting someone to misread it.
    """
    width = bucket_width(hours)
    rollup = hours > ROLLUP_ABOVE_HOURS
    sql = TREND_ROLLUP if rollup else TREND_RAW
    with conn.cursor() as cur:
        cur.execute(sql, {"code": code, "param": param,
                          "hours": hours, "width": width})
        return cur.fetchall(), width, "hourly rollup" if rollup else "readings"
