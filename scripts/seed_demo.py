"""
Fill the database with realistic history, so reports and queries have
something to work on.

The equivalent of Meril EMS's `seed:demo`: it writes the same rows the
poller would have written, so every query above it exercises the real
shape of the data. Safe to re-run, and it never runs as part of the
application.

    uv run python scripts/seed_demo.py              # 30 days
    uv run python scripts/seed_demo.py --days 90
    uv run python scripts/seed_demo.py --clear      # remove demo rows only

It seeds BOTH product shapes, because they behave differently and the
difference is the thing worth understanding:

  DEMO-DP-xx    differential pressure, an INSTANTANEOUS value. Wanders
                around a setpoint, with door-open excursions that go
                negative. Summarised by min/max/avg; consumption() refuses
                it.

  DEMO-EM-xx    an energy meter, a COUNTER. Only ever increases, faster
                during shifts than at night, and one of them is given a
                deliberate meter replacement so counter_reset can be seen.
                Summarised by first/last; consumption() is meaningful.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

import psycopg  # noqa: E402

DP_ROOMS = [("DEMO-DP-01", "Demo Corridor", 10.0),
            ("DEMO-DP-02", "Demo Airlock", 12.5),
            ("DEMO-DP-03", "Demo Filling", 15.0)]

METERS = [("DEMO-EM-01", "Demo Main Incomer", 420.0),
          ("DEMO-EM-02", "Demo Chiller Plant", 180.0)]

PARAMS = [
    ("dp", "Differential pressure", "Pa", "instantaneous", 0),
    ("kwh_import", "Energy imported", "kWh", "counter", 2),
]


def dsn() -> str:
    pw = ROOT / ".local" / "app_password"
    if not pw.exists():
        raise SystemExit("No database yet. Run scripts/setup_postgres.py first.")
    return (f"postgresql://jetpace_app:{pw.read_text().strip()}"
            f"@127.0.0.1:5433/jetpace")


def ensure_assets(cur) -> dict[str, int]:
    cur.execute("SELECT id FROM tenant WHERE slug = 'jetpace'")
    row = cur.fetchone()
    if not row:
        raise SystemExit("Run scripts/seed_prototype.py first.")
    tenant_id = row[0]

    cur.execute("SELECT id FROM site WHERE tenant_id = %s ORDER BY id LIMIT 1",
                (tenant_id,))
    site_id = cur.fetchone()[0]
    cur.execute("SELECT id FROM gateway WHERE tenant_id = %s ORDER BY id LIMIT 1",
                (tenant_id,))
    gateway_id = cur.fetchone()[0]

    for key, label, unit, agg, dec in PARAMS:
        cur.execute(
            """INSERT INTO parameter (key, label, unit, aggregation, decimals)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label""",
            (key, label, unit, agg, dec))

    cur.execute(
        """INSERT INTO device_model (code, revision, vendor, kind, datasheet_ref)
           VALUES ('DEMO-METER','1','Jetpace','energy','demo')
           ON CONFLICT (code, revision) DO UPDATE SET vendor = EXCLUDED.vendor
           RETURNING id""")
    meter_model = cur.fetchone()[0]
    cur.execute("SELECT id FROM device_model WHERE code = 'iDP-100' LIMIT 1")
    dp_model = cur.fetchone()[0]

    ids: dict[str, int] = {}
    unit_id = 50                      # well clear of the real devices
    for code, name, _ in DP_ROOMS + METERS:
        model = dp_model if code.startswith("DEMO-DP") else meter_model
        cur.execute(
            """INSERT INTO device (tenant_id, site_id, gateway_id, model_id,
                                   code, name, unit_id, commissioned_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s, now() - interval '400 days')
               ON CONFLICT (tenant_id, code)
                 DO UPDATE SET name = EXCLUDED.name, retired_at = NULL
               RETURNING id""",
            (tenant_id, site_id, gateway_id, model, code, name, unit_id))
        ids[code] = cur.fetchone()[0]
        unit_id += 1

    cur.execute("SELECT key, id FROM parameter")
    ids.update({f"param:{k}": v for k, v in cur.fetchall()})
    return ids


def rows(ids: dict[str, int], days: int):
    """Yield (ts, device_id, param, value, quality) for every minute."""
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    dp_param = ids["param:dp"]
    kwh_param = ids["param:kwh_import"]

    counters = {code: base * 24 * 300 for code, _, base in METERS}
    # one meter is replaced two thirds of the way through, so its counter
    # restarts from zero - which is what counter_reset exists to catch
    replacement_at = start + (end - start) * 2 // 3
    replaced = False

    door_until = {code: start for code, _, _ in DP_ROOMS}
    ts = start
    while ts < end:
        hour = ts.astimezone().hour

        for code, _, setpoint in DP_ROOMS:
            if ts < door_until[code]:
                value = -3.0 + random.gauss(0, 0.6)
            else:
                value = setpoint + random.gauss(0, 0.5)
                if random.random() < 0.0008:
                    door_until[code] = ts + timedelta(
                        seconds=random.randint(120, 900))
            yield ts, ids[code], dp_param, round(value), 0

        # a shift pattern: heavy 08:00-20:00, light overnight
        load = 0.35 + 0.65 * max(0.0, math.sin((hour - 6) / 24 * 2 * math.pi))
        for code, _, base in METERS:
            if code == "DEMO-EM-02" and ts >= replacement_at and not replaced:
                counters[code] = 0.0
            counters[code] += base * load / 60.0
            yield ts, ids[code], kwh_param, round(counters[code], 3), 0

        if ts >= replacement_at:
            replaced = True
        ts += timedelta(minutes=1)


def clear(cur) -> None:
    """
    Take the demo devices out of the system.

    RETIRED, NOT DELETED, which is what migrations/0001 says a removed
    device is: "a device removed from the sheet is switched off, not
    deleted: it stops being polled and drops out of reports, but its
    history stays". Every query in the application filters on
    `retired_at IS NULL`, so a retired device vanishes from the overview,
    the device list, the facility panel, the exceptions panel and the
    exports in one statement - and a reading already written still has a
    device row to explain what it was.

    Deleting the rows would be worse in two ways. `reading` carries
    `device_id` with no foreign key - it is a hypertable, and the
    constraint would cost on every insert - so the readings would be
    orphaned rather than removed, pointing at an id that no longer
    resolves. And the decision would be irreversible, when the whole
    reason to keep demo data around is that it is useful again the moment
    somebody wants to see what a month of history looks like.

    This used to print the DELETE and do nothing, which meant `--clear`
    did not clear. Retiring is within jetpace_app's own grants - it has
    UPDATE on every table, and only `reading` has UPDATE and DELETE
    revoked - so this needs no superuser.
    """
    cur.execute("SELECT id, code FROM device "
                " WHERE code LIKE 'DEMO-%' AND retired_at IS NULL")
    found = cur.fetchall()
    if not found:
        print("  no demo devices are in service")
        return

    cur.execute("UPDATE device SET retired_at = now() "
                " WHERE code LIKE 'DEMO-%' AND retired_at IS NULL")
    print(f"  retired {cur.rowcount} demo device(s): "
          f"{', '.join(code for _, code in found)}")
    print("  They are out of every page and every export. Their readings "
          "stay; re-run this script without --clear to put them back.")

    # And if somebody genuinely wants the rows gone - a validated install
    # that must carry no fabricated data at all - say how. jetpace_app
    # cannot do it: `reading` is append-only for the application role by
    # design, which is a CLAUDE.md invariant. Dropping the chunks is not
    # an option either, because real readings share them.
    ids = ", ".join(str(i) for i, _ in found)
    print()
    print("  To purge the readings as well, connect as jetpace_super:")
    print(f"    DELETE FROM reading WHERE device_id IN ({ids});")
    print(f"    DELETE FROM device WHERE id IN ({ids});")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()

    with psycopg.connect(dsn()) as conn:
        cur = conn.cursor()
        if args.clear:
            clear(cur)
            return

        ids = ensure_assets(cur)
        conn.commit()

        print(f"  generating {args.days} days at 1-minute resolution")
        n = 0
        with cur.copy("COPY reading (ts, device_id, param, value, quality) "
                      "FROM STDIN") as copy:
            for row in rows(ids, args.days):
                copy.write_row(row)
                n += 1
        conn.commit()
        print(f"  wrote {n:,} readings")

    # refreshing an aggregate cannot run inside a transaction block
    with psycopg.connect(dsn(), autocommit=True) as conn:
        for view in ("reading_hourly", "reading_daily"):
            print(f"  refreshing {view}")
            conn.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")

    with psycopg.connect(dsn()) as conn:
        for label, sql in [
            ("raw readings", "SELECT count(*) FROM reading"),
            ("hourly rows", "SELECT count(*) FROM reading_hourly"),
            ("daily rows", "SELECT count(*) FROM reading_daily"),
        ]:
            print(f"  {label:<14} {conn.execute(sql).fetchone()[0]:,}")


if __name__ == "__main__":
    main()
