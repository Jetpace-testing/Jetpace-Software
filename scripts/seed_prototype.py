"""
Put the contents of config/prototype.toml into the database.

This is a step towards moving the device list out of the file altogether -
which is where it belongs, with the dry-run diff and reversible apply that
Meril EMS already does well. For now the file is still the source of truth
and this script makes the database agree with it.

Idempotent: run it as often as you like. Devices are matched on
(tenant, code), which is stable forever, so renaming a room in the file
updates the name and keeps the readings.

    uv run python scripts/seed_prototype.py
    uv run python scripts/seed_prototype.py --show
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

import psycopg  # noqa: E402

# What each parameter means. `aggregation` decides which rollup columns are
# meaningful: a counter is summarised by first/last, an instantaneous value
# by min/max/avg.
PARAMETERS = [
    ("dp", "Differential pressure", "Pa", "instantaneous", 0),
    ("dp_unit", "Pressure unit selector", "", "instantaneous", 0),
]

MODELS = [
    ("iDP-100", "2026-10", "Jetpace", "pressure",
     "Modbus Mapping/Differential Pressure Transmitter Modbus Mapping.pdf"),
]


def dsn() -> str:
    pw = ROOT / ".local" / "app_password"
    if not pw.exists():
        raise SystemExit("No database yet. Run scripts/setup_postgres.py first.")
    return (f"postgresql://jetpace_app:{pw.read_text().strip()}"
            f"@127.0.0.1:5433/jetpace")


def seed(cfg: dict, conn: psycopg.Connection) -> None:
    site_cfg = cfg["site"]
    cur = conn.cursor()

    cur.execute(
        """INSERT INTO tenant (slug, name) VALUES (%s, %s)
           ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
           RETURNING id""",
        (site_cfg["tenant"], site_cfg["tenant"].title()))
    tenant_id = cur.fetchone()[0]

    cur.execute(
        """INSERT INTO site (tenant_id, name, timezone) VALUES (%s, %s, %s)
           ON CONFLICT (tenant_id, name) DO UPDATE SET timezone = EXCLUDED.timezone
           RETURNING id""",
        (tenant_id, site_cfg["name"], site_cfg["timezone"]))
    site_id = cur.fetchone()[0]

    for key, label, unit, agg, decimals in PARAMETERS:
        cur.execute(
            """INSERT INTO parameter (key, label, unit, aggregation, decimals)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label""",
            (key, label, unit, agg, decimals))

    model_ids: dict[str, int] = {}
    for code, revision, vendor, kind, ref in MODELS:
        cur.execute(
            """INSERT INTO device_model (code, revision, vendor, kind, datasheet_ref)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (code, revision) DO UPDATE SET vendor = EXCLUDED.vendor
               RETURNING id""",
            (code, revision, vendor, kind, ref))
        model_ids[code] = cur.fetchone()[0]

    devices = 0
    for g in cfg.get("gateway", []):
        cur.execute(
            """INSERT INTO gateway (tenant_id, site_id, label, host, port,
                                    timeout_ms, gap_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, host, port)
                 DO UPDATE SET label = EXCLUDED.label,
                               timeout_ms = EXCLUDED.timeout_ms,
                               gap_ms = EXCLUDED.gap_ms
               RETURNING id""",
            (tenant_id, site_id, g["label"], g["host"], int(g.get("port", 502)),
             int(float(g.get("timeout_s", 2.0)) * 1000),
             int(float(g.get("gap_s", 0.025)) * 1000)))
        gateway_id = cur.fetchone()[0]

        for d in g.get("device", []):
            model = d["model"]
            if model not in model_ids:
                raise SystemExit(f"device {d['code']}: unknown model {model!r}")
            cur.execute(
                """INSERT INTO device (tenant_id, site_id, gateway_id, model_id,
                                       code, name, unit_id, scan_class,
                                       commissioned_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'critical', now())
                   ON CONFLICT (tenant_id, code)
                     DO UPDATE SET name = EXCLUDED.name,
                                   unit_id = EXCLUDED.unit_id,
                                   retired_at = NULL
                   RETURNING id""",
                (tenant_id, site_id, gateway_id, model_ids[model],
                 d["code"], d["name"], int(d["unit_id"])))
            device_id = cur.fetchone()[0]
            devices += 1

            for point in cfg["model"][model]["point"]:
                cur.execute(
                    """INSERT INTO device_parameter (device_id, param, scale)
                       SELECT %s, id, %s FROM parameter WHERE key = %s
                       ON CONFLICT (device_id, param)
                         DO UPDATE SET scale = EXCLUDED.scale""",
                    (device_id, point.get("scale", 1.0), point["param"]))

    conn.commit()
    print(f"  tenant {site_cfg['tenant']} / site {site_cfg['name']}")
    print(f"  {len(PARAMETERS)} parameter(s), {len(MODELS)} model(s), "
          f"{devices} device(s)")


def show(conn: psycopg.Connection) -> None:
    cur = conn.cursor()
    cur.execute("""
        SELECT d.code, d.name, g.label, d.unit_id, m.code, d.scan_class,
               (SELECT count(*) FROM device_parameter dp WHERE dp.device_id = d.id)
        FROM device d
        JOIN gateway g ON g.id = d.gateway_id
        JOIN device_model m ON m.id = d.model_id
        WHERE d.retired_at IS NULL
        ORDER BY g.label, d.unit_id
    """)
    print(f"\n  {'code':<10}{'name':<12}{'gateway':<10}{'unit':>5}  "
          f"{'model':<10}{'class':<13}{'params':>6}")
    for code, name, gw, unit, model, cls, n in cur.fetchall():
        print(f"  {code:<10}{name:<12}{gw:<10}{unit:>5}  {model:<10}{cls:<13}{n:>6}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path,
                    default=ROOT / "config" / "prototype.toml")
    ap.add_argument("--show", action="store_true",
                    help="print what is in the database and exit")
    args = ap.parse_args()

    with psycopg.connect(dsn()) as conn:
        if args.show:
            return show(conn)
        cfg = tomllib.loads(args.config.read_text(encoding="utf-8"))
        seed(cfg, conn)
        show(conn)


if __name__ == "__main__":
    main()
