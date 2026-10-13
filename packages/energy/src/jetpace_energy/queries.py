"""
Every query the energy product runs.

Moved out of jetpace_core.web.queries. Both of these contain the literal
`p.key = 'kwh_import'`, and ENERGY_PERIOD calls `consumption()` - the
billing function - so they describe one product's arithmetic and belong
with that product.

The connection comes from core - `queries.connect` - so exactly one piece
of code decides the row factory and the connect timeout.

ENERGY_PERIOD goes through `consumption()` rather than subtracting two
readings itself, because that function is where the statuses live
(`ok`, `partial_new_meter`, `counter_reset`, `stale_open`,
`not_a_counter`). architecture.md §7 requires a figure the system cannot
defend to come back with a status rather than a plausible guess, and
re-implementing the subtraction here would be exactly that guess.
"""

from __future__ import annotations

import psycopg

# ---------------------------------------------------------------------------
# daily consumption, straight from the daily rollup
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


# ---------------------------------------------------------------------------
# a billing period - through consumption(), so the statuses are the real ones
# ---------------------------------------------------------------------------

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
# running them
# ---------------------------------------------------------------------------

def energy_daily(conn: psycopg.Connection, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(ENERGY_DAILY, {"days": days})
        return cur.fetchall()


def energy_period(conn: psycopg.Connection, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(ENERGY_PERIOD, {"days": days})
        return cur.fetchall()
