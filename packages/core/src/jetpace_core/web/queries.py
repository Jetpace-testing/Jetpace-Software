"""
Every query the dashboard runs, in one file.

SQL lives here rather than scattered through request handlers, for three
reasons: it can be read as a set and reviewed as a set, it can be run by
hand in psql when something looks wrong, and the handlers stay about HTTP.

All of it reads, still - but no longer because it has to. The app role has
held `SELECT, INSERT, UPDATE, DELETE` on every table except `reading`
since the database was first provisioned (setup_postgres.py), so the
absence of a write path here has always been a choice about what was
built rather than a privilege the application lacks. A note in the
add-device modal claimed otherwise and was wrong.

The first writes arrive with the commissioning form: a device row, its
parameters, its limits and an audit row. They live in `writes.py`, in a
separate file and behind a single transaction, because a statement that
changes the configuration of a validated instrument should be impossible
to confuse at a glance with one that reads a number.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

# A device that has not reported for longer than this is shown as offline.
# Deliberately generous: the prototype's scan interval is 2 s, but a device
# in offline backoff is skipped for several passes, so a tight threshold
# would flap. Stage 2b replaces this entirely with a retained MQTT status
# topic, at which point "offline" stops being a guess about silence.
OFFLINE_AFTER = "interval '90 seconds'"


# A `Summary` dataclass used to sit here declaring rooms_in_spec,
# rooms_total and excursions_24h beside the shared counts. Nothing ever
# referenced it - summary() returns a plain dict - so it was both dead and
# a description of one product's screen living in the shared layer. The
# shared summary is defined by SUMMARY below and nothing else.


# What /healthz asks, and deliberately not SUMMARY.
#
# The supervisor polls the health endpoint every two seconds while a
# release is on trial, so this has to stay cheap. SUMMARY counts every
# reading in the last 24 hours - on a site logging 400 meters that is
# tens of millions of rows to answer a question health does not ask.
#
# max(ts) on a hypertable is the one thing health genuinely needs, and
# TimescaleDB answers it from chunk metadata rather than by scanning.
#
# The age is computed by the DATABASE, not by subtracting in Python. The
# application and the database can disagree about the time - different
# clocks, and in a frozen install a different idea of the timezone - and
# a health check that reports a negative age because of clock skew is
# worse than no health check.
HEALTH = """
WITH newest AS (SELECT max(ts) AS ts FROM reading)
SELECT n.ts                                       AS newest,
       EXTRACT(EPOCH FROM (now() - n.ts))::float  AS age_s,
       (SELECT count(*) FROM device
         WHERE retired_at IS NULL)                AS devices
FROM   newest n
"""


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

# ---------------------------------------------------------------------------
# the live table
# ---------------------------------------------------------------------------

LIVE = f"""
-- No DISTINCT ON any more. It existed to take the newest of a device's
-- readings for each parameter; the LATERAL below does that with LIMIT 1,
-- and device_parameter is unique on (device_id, param), so there is
-- nothing left to de-duplicate. Leaving it would also be a syntax error:
-- PostgreSQL requires the DISTINCT ON expressions to lead the ORDER BY,
-- and this now orders by code so the table arrives in the order a person
-- reads it rather than in insertion order.
SELECT d.code,
       d.name,
       g.label                                   AS gateway,
       d.unit_id,
       p.key                                     AS param,
       -- The parameter's human label, for the trend panel's tabs. Not
       -- product knowledge: `label` is whatever the site called the
       -- measurement, the same string the device page already shows.
       p.label                                   AS param_label,
       p.unit,
       p.decimals,
       p.aggregation,
       l.value,
       l.ts,
       l.ts > now() - {OFFLINE_AFTER}            AS online,
       -- GREATEST(0, ...) because TWO clocks are involved: the poller
       -- timestamps the reading in Python, now() is the database's
       -- transaction start, and a fraction of a second of skew between
       -- them renders as "-1 s". Harmless here; across a WAN to an edge
       -- agent it would not be, which is why §14 insists on NTP.
       GREATEST(0, EXTRACT(EPOCH FROM (now() - l.ts)))::int AS age_s
FROM   device d
JOIN   gateway g           ON g.id = d.gateway_id
-- DRIVEN BY CONFIGURATION, not by readings.
--
-- This was an inner join on `reading`, which meant a device that had
-- never reported was ABSENT from the overview's table - not offline,
-- not blank, gone. An operator commissions an instrument, it does not
-- answer because the bus is not wired yet, and the device they just
-- created is nowhere on the screen with nothing to say why. That is the
-- same failure the poller takes care to avoid with `Plan.unreadable`,
-- and the commissioning form promised the opposite in writing: "it is
-- in the device table, offline, with no readings".
--
-- DEVICES and FACILITY were already written this way, with a LEFT JOIN
-- LATERAL; this query was the only one left asserting that a device
-- exists if and only if it has spoken.
--
-- Taking the parameter list from device_parameter rather than from the
-- readings also says something more useful: these are the values the
-- device is CONFIGURED to report, so one that has stopped sending a
-- parameter shows it blank instead of silently narrowing the table.
--
-- It is cheaper too. DISTINCT ON over the hypertable sorted every
-- reading a device had ever produced to take the first row; the lateral
-- is one index seek per (device, parameter) on reading_pk, which is
-- (device_id, param, ts DESC).
LEFT   JOIN device_parameter dp ON dp.device_id = d.id AND dp.enabled
LEFT   JOIN parameter p         ON p.id = dp.param
LEFT   JOIN LATERAL (
         SELECT r.value, r.ts
         FROM   reading r
         WHERE  r.device_id = d.id AND r.param = p.id
         ORDER  BY r.ts DESC
         LIMIT  1
       ) l ON true
WHERE  d.retired_at IS NULL
ORDER  BY d.code, p.key
"""


# The pressure trend, the excursion count and the two energy queries used
# to sit here. They are now jetpace_environment.queries and
# jetpace_energy.queries, because each of them named a parameter key and
# so could only ever serve one product.


# ---------------------------------------------------------------------------
# the device list and one device's detail
#
# Nothing below mentions a parameter key, and that is the property worth
# protecting: these queries work for a pressure transmitter, an energy
# meter or a particle counter without being told which they are serving.
# A key appearing here means the shell has quietly become one product's
# shell again, which is what test_web_shell.py checks.
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
       -- ct_ratio and pt_ratio were selected here and rendered nowhere.
       -- They are current- and potential-transformer ratios, which only an
       -- electricity meter has, so a shell query asking for them is the
       -- shell knowing about the energy product. The columns stay in the
       -- schema; energy selects them when it has a page that shows them.
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


def health(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(HEALTH)
        return cur.fetchone() or {}


def live(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(LIVE)
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


# ---------------------------------------------------------------------------
# the connectivity breakdown behind the distribution donut
#
# DEVICE-LEVEL ONLY, and that is the point. SUMMARY counts every reading
# in the last 24 hours, which on a 400-meter site is tens of millions of
# rows; this panel refreshes beside the live table and must not carry
# that. Every figure below is a count over `device` with one indexed
# lookup each, so it stays cheap however much history has accumulated.
#
# THREE STATES, NOT TWO. "Offline" in the design means one thing; here a
# device that has never reported at all is a different situation from one
# that reported this morning and stopped - the first is a commissioning
# fault, the second is a fault in the field - and a donut that merges
# them tells the operator to go looking in the wrong place.
# ---------------------------------------------------------------------------

DISTRIBUTION = f"""
WITH seen AS (
    SELECT d.id, l.ts AS newest
    FROM   device d
    LEFT   JOIN LATERAL (
             SELECT r.ts FROM reading r
             WHERE  r.device_id = d.id
             ORDER  BY r.ts DESC LIMIT 1
           ) l ON true
    WHERE  d.retired_at IS NULL
)
SELECT count(*)                                                  AS devices,
       count(*) FILTER (WHERE newest > now() - {OFFLINE_AFTER})  AS online,
       count(*) FILTER (WHERE newest IS NOT NULL
                          AND newest <= now() - {OFFLINE_AFTER}) AS silent,
       count(*) FILTER (WHERE newest IS NULL)                    AS never_seen,
       (SELECT count(*) FROM gateway WHERE enabled)              AS gateways,
       (SELECT count(DISTINCT d.gateway_id)
          FROM device d
          JOIN reading r ON r.device_id = d.id
                        AND r.ts > now() - {OFFLINE_AFTER}
         WHERE d.retired_at IS NULL)                             AS gateways_live
FROM   seen
"""


# ---------------------------------------------------------------------------
# where the devices are, and how they are
#
# The design draws a floor plan: five named zones at hand-chosen
# coordinates with device dots scattered over them. There is no room,
# zone or coordinate anywhere in this schema - a device belongs to a
# gateway, and a gateway to a site - so those positions could only be
# invented, and an invented floor plan shown to a customer is a promise
# that the software knows where their instruments are.
#
# A gateway IS the real grouping: it is one serial bus or one converter,
# which in practice is one area of the plant. So the panel answers the
# design's question - which parts of the facility are healthy - from the
# grouping that exists, and lays the zones out rather than positioning
# them.
# ---------------------------------------------------------------------------

FACILITY = f"""
SELECT g.label                                   AS gateway,
       g.host,
       g.port,
       g.enabled,
       s.name                                    AS site,
       d.code,
       d.name,
       l.ts                                      AS newest,
       l.ts > now() - {OFFLINE_AFTER}            AS online,
       GREATEST(0, EXTRACT(EPOCH FROM (now() - l.ts)))::int AS age_s
FROM   device d
JOIN   gateway g ON g.id = d.gateway_id
JOIN   site s    ON s.id = d.site_id
LEFT   JOIN LATERAL (
         SELECT r.ts FROM reading r
         WHERE  r.device_id = d.id
         ORDER  BY r.ts DESC LIMIT 1
       ) l ON true
WHERE  d.retired_at IS NULL
ORDER  BY g.label, d.code
"""


# ---------------------------------------------------------------------------
# exceptions - what the shell can honestly say has gone wrong
#
# THERE IS NO ALARM MODEL. No table of limits, no alarm row, no
# acknowledgement, no severity. The design's "Recent Alarms" panel is a
# subsystem this install does not have (architecture.md §10), and the
# only two kinds of exception the shared layer genuinely knows about are
# below. Both are facts about data that has or has not arrived, which is
# precisely what the shell is entitled to have an opinion on.
#
# A product that does have limits - a cleanroom has a pressure cascade
# with a specification - contributes its own through Product.events.
# ---------------------------------------------------------------------------

# A device that has stopped answering, or never started.
SILENCE = f"""
SELECT d.code,
       d.name,
       g.label                                   AS gateway,
       l.ts                                      AS newest,
       GREATEST(0, EXTRACT(EPOCH FROM (now() - l.ts)))::int AS age_s
FROM   device d
JOIN   gateway g ON g.id = d.gateway_id
LEFT   JOIN LATERAL (
         SELECT r.ts FROM reading r
         WHERE  r.device_id = d.id
         ORDER  BY r.ts DESC LIMIT 1
       ) l ON true
WHERE  d.retired_at IS NULL
  AND  (l.ts IS NULL OR l.ts <= now() - {OFFLINE_AFTER})
ORDER  BY l.ts DESC NULLS LAST
LIMIT  %(limit)s
"""

# Samples the poller itself flagged: 1 substituted from a device's own
# log, 2 suspect. Grouped per device and parameter, because six hundred
# consecutive suspect samples from one transmitter is ONE problem and a
# list that shows it six hundred times has buried everything else.
#
# WINDOW DELIBERATELY SHORT. `quality` has no index, so this is a scan of
# whatever window it is given; six hours is one or two hypertable chunks
# and stays affordable. Before a 400-meter site runs this, it wants
# `CREATE INDEX ... ON reading (ts DESC) WHERE quality <> 0` - a partial
# index, which is cheap because almost no row qualifies. Recorded in
# architecture.md rather than added here, because a schema change to a
# running install is not something to slip into a screen build.
SUSPECT = """
SELECT d.code,
       d.name,
       g.label                                 AS gateway,
       p.label                                 AS parameter,
       p.unit,
       p.decimals,
       max(r.ts)                               AS newest,
       count(*)                                AS samples,
       max(r.quality)                          AS worst,
       (array_agg(r.value ORDER BY r.ts DESC))[1] AS value
FROM   reading r
JOIN   device d    ON d.id = r.device_id AND d.retired_at IS NULL
JOIN   gateway g   ON g.id = d.gateway_id
JOIN   parameter p ON p.id = r.param
WHERE  r.ts > now() - %(hours)s * interval '1 hour'
  AND  r.quality <> 0
GROUP  BY d.code, d.name, g.label, p.label, p.unit, p.decimals
ORDER  BY max(r.ts) DESC
LIMIT  %(limit)s
"""

SUSPECT_HOURS = 6


# ---------------------------------------------------------------------------
# the export behind the reports panel
#
# The design lists three finished PDF reports with file sizes. There is
# no report generator, no scheduler and no stored file in this install,
# so three named PDFs with download buttons would be three buttons that
# 404. What does exist is the readings themselves, and the thing an
# operator actually asks for at this stage is "give me the numbers" - so
# the panel offers real exports, generated on request.
#
# STREAMED AND BOUNDED. A day of a 400-meter site is millions of rows;
# this is read in server-side cursor batches and capped, so an export can
# neither exhaust memory nor run until the browser gives up.
# ---------------------------------------------------------------------------

EXPORT_READINGS = """
SELECT r.ts,
       d.code     AS device,
       d.name     AS device_name,
       g.label    AS gateway,
       p.key      AS parameter,
       p.unit,
       r.value,
       r.quality
FROM   reading r
JOIN   device d    ON d.id = r.device_id AND d.retired_at IS NULL
JOIN   gateway g   ON g.id = d.gateway_id
JOIN   parameter p ON p.id = r.param
WHERE  r.ts > now() - %(hours)s * interval '1 hour'
ORDER  BY r.ts, d.code, p.key
LIMIT  %(limit)s
"""

EXPORT_DEVICES = """
SELECT d.code,
       d.name,
       g.label    AS gateway,
       g.host,
       g.port,
       d.unit_id,
       m.code     AS model,
       m.revision AS model_revision,
       m.vendor,
       m.kind,
       s.name     AS site,
       d.scan_class,
       d.commissioned_at
FROM   device d
JOIN   gateway g      ON g.id = d.gateway_id
JOIN   device_model m ON m.id = d.model_id
JOIN   site s         ON s.id = d.site_id
WHERE  d.retired_at IS NULL
ORDER  BY d.code
"""


# ---------------------------------------------------------------------------

def distribution(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(DISTRIBUTION)
        return cur.fetchone() or {}


def facility(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(FACILITY)
        return cur.fetchall()


def silence(conn: psycopg.Connection, limit: int = 12) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(SILENCE, {"limit": limit})
        return cur.fetchall()


def suspect(conn: psycopg.Connection, hours: int = SUSPECT_HOURS,
            limit: int = 12) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(SUSPECT, {"hours": hours, "limit": limit})
        return cur.fetchall()


def export_readings(conn: psycopg.Connection, hours: float, limit: int):
    """
    Readings for a window, as a server-side cursor.

    A named cursor so psycopg fetches in batches rather than building the
    whole result in memory first - the difference between an export that
    streams and one that takes the dashboard down with it.
    """
    cur = conn.cursor(name="export_readings")
    cur.itersize = 2000
    cur.execute(EXPORT_READINGS, {"hours": hours, "limit": limit})
    return cur


def export_devices(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(EXPORT_DEVICES)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# the shape of one device's day, for the parameter cards' sparklines
#
# FROM THE ROLLUP, not from raw. A day of one device at a 2-second scan
# is 43,200 rows to draw a 24-pixel-tall line; the same day from
# reading_hourly is 24. The sparkline cannot tell the difference and the
# database very much can.
#
# The min and max come back with it because the cards show the range the
# day actually covered. That is an OBSERVED range, not a specification, so
# the card says "last 24 h" and never "in spec". The design's cards say
# "Range 18-24 °C", which is a specification it was given with its sample
# data; claiming one here would be inventing the number that matters most.
#
# A set point for the parameter is now a thing that CAN exist - 0005 adds
# device_limit and the commissioning form collects LSP and HSP - but it is
# a separate query (`device_limits` below) and a separate claim. A card
# showing both is showing two different kinds of fact, and has to label
# them differently or it is worse than showing one.
# ---------------------------------------------------------------------------

DEVICE_SHAPE = """
SELECT p.key,
       h.bucket,
       h.avg_value AS avg,
       h.min_value AS min,
       h.max_value AS max
FROM   reading_hourly h
JOIN   device d    ON d.id = h.device_id AND d.code = %(code)s
JOIN   parameter p ON p.id = h.param
WHERE  h.bucket > now() - interval '24 hours'
ORDER  BY p.key, h.bucket
"""


def device_shape(conn: psycopg.Connection, code: str) -> dict[str, dict]:
    """
    Per parameter: the day's hourly averages, and the day's range.

    Returned keyed by parameter so a template can look one up beside the
    current value without a second pass.
    """
    with conn.cursor() as cur:
        cur.execute(DEVICE_SHAPE, {"code": code})
        rows = cur.fetchall()

    shaped: dict[str, dict] = {}
    for r in rows:
        s = shaped.setdefault(r["key"], {"points": [], "lo": None, "hi": None})
        s["points"].append(float(r["avg"]))
        lo, hi = float(r["min"]), float(r["max"])
        s["lo"] = lo if s["lo"] is None else min(s["lo"], lo)
        s["hi"] = hi if s["hi"] is None else max(s["hi"], hi)
    return shaped


# ---------------------------------------------------------------------------
# limits
#
# `device_limit` is append-only (migrations/0005): changing a set point
# inserts a row and the one in force is the most recent by set_at. Every
# read of it is therefore a "latest per group" query, which is DISTINCT ON
# in PostgreSQL - the index device_limit_current is (device_id, param,
# set_at DESC) precisely so this is a walk of one entry per group rather
# than a sort of the whole history.
#
# Why not a `current` flag, or a `superseded_at` the writer stamps: both
# need UPDATE on the table, and the grant deliberately does not have it.
# The ordering IS the state, which means there is no way to get the two
# out of step.
# ---------------------------------------------------------------------------

LIMITS = """
SELECT DISTINCT ON (l.device_id, l.param)
       d.code                AS device,
       p.key                 AS param,
       l.low,
       l.high,
       l.set_at,
       l.set_by,
       l.reason
FROM   device_limit l
JOIN   device d    ON d.id = l.device_id
JOIN   parameter p ON p.id = l.param
WHERE  d.retired_at IS NULL
ORDER  BY l.device_id, l.param, l.set_at DESC
"""

DEVICE_LIMITS = """
SELECT DISTINCT ON (l.param)
       p.key     AS param,
       l.low,
       l.high,
       l.set_at,
       l.set_by,
       l.reason
FROM   device_limit l
JOIN   device d    ON d.id = l.device_id AND d.code = %(code)s
JOIN   parameter p ON p.id = l.param
ORDER  BY l.param, l.set_at DESC
"""


TENANT = "SELECT id FROM tenant ORDER BY id LIMIT 1"


def tenant_id(conn: psycopg.Connection) -> int:
    """
    The tenant this install serves.

    One per install - architecture.md section 4 says every table carries
    tenant_id from the first migration because retrofitting tenancy is
    the refactor that is always attempted under commercial pressure, not
    because anything multi-tenant exists yet. Zero is returned for an
    empty database so a caller that only needs it to scope a LIKE does
    not have to handle None.
    """
    with conn.cursor() as cur:
        cur.execute(TENANT)
        row = cur.fetchone()
    return row["id"] if row else 0


def limits(conn: psycopg.Connection) -> dict[tuple[str, str], dict]:
    """
    Every limit in force, keyed by (device code, parameter key).

    A dict rather than a list because every caller is asking "does this
    device-and-parameter have a limit", once per row it is already
    rendering, and a list would make that a scan per row.
    """
    with conn.cursor() as cur:
        cur.execute(LIMITS)
        return {(r["device"], r["param"]): r for r in cur.fetchall()}


def device_limits(conn: psycopg.Connection, code: str) -> dict[str, dict]:
    """
    One device's limits in force, keyed by parameter key.

    Retired devices included, unlike `limits()`: this is reached from a
    device's own page, and a retired device's page should still say what
    it was held to.
    """
    with conn.cursor() as cur:
        cur.execute(DEVICE_LIMITS, {"code": code})
        return {r["param"]: r for r in cur.fetchall()}
