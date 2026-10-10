"""
Run the poller against whatever config/prototype.toml describes.

    python scripts/run_poller.py                   # print to the console
    python scripts/run_poller.py --sink postgres   # write to the database
    python scripts/run_poller.py --quiet           # counts only

Start the simulator first:

    python tools/sim/idp100_sim.py --units 3

Ctrl+C stops it cleanly.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

from jetpace_core.modbus_client import tcp_client_factory          # noqa: E402
from jetpace_core.poller import DeviceSpec, GatewaySpec, Poller, PointSpec  # noqa: E402
from jetpace_core.mqtt import Site                                 # noqa: E402
from jetpace_core.sinks import ConsoleSink, MqttSink, PostgresSink # noqa: E402


def load_config(path: Path) -> list[GatewaySpec]:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))

    models: dict[str, tuple[PointSpec, ...]] = {}
    for name, body in cfg.get("model", {}).items():
        models[name] = tuple(
            PointSpec(param=p["param"], address=p["address"],
                      kind=p["kind"], scale=float(p.get("scale", 1.0)))
            for p in body.get("point", [])
        )

    gateways: list[GatewaySpec] = []
    for g in cfg.get("gateway", []):
        devices = []
        for d in g.get("device", []):
            model = d["model"]
            if model not in models:
                raise SystemExit(f"device {d['code']}: no [model.{model}] section")
            devices.append(DeviceSpec(
                code=d["code"], name=d["name"],
                unit_id=int(d["unit_id"]), points=models[model],
            ))
        gateways.append(GatewaySpec(
            label=g["label"], host=g["host"], port=int(g.get("port", 502)),
            timeout_s=float(g.get("timeout_s", 2.0)),
            gap_s=float(g.get("gap_s", 0.025)),
            interval_s=float(g.get("interval_s", 1.0)),
            offline_backoff_cycles=int(g.get("offline_backoff_cycles", 5)),
            devices=tuple(devices),
        ))
    return gateways


def broker_credentials() -> tuple[str | None, str | None]:
    """Read the broker password the setup script generated, if there is one."""
    pw = ROOT / ".local" / "mqtt_password"
    if pw.exists():
        return "jetpace_app", pw.read_text().strip()
    return None, None


def postgres_dsn() -> str:
    pw_file = ROOT / ".local" / "app_password"
    if not pw_file.exists():
        raise SystemExit(
            "No database yet. Run:\n"
            "  python scripts/setup_postgres.py --pg-zip <path to the binaries zip>"
        )
    return (f"postgresql://jetpace_app:{pw_file.read_text().strip()}"
            f"@127.0.0.1:5433/jetpace")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "prototype.toml")
    ap.add_argument("--sink", choices=("console", "postgres", "mqtt"),
                    default="console")
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--broker-port", type=int, default=1887)
    ap.add_argument("--tenant", default="jetpace")
    ap.add_argument("--site", default="prototype")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--seconds", type=float, help="stop after this long (for tests)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    gateways = load_config(args.config)
    # The whole point of the Sink Protocol: three implementations, one
    # construction site, and nothing below this line knows the difference.
    if args.sink == "postgres":
        sink = PostgresSink(postgres_dsn())
    elif args.sink == "mqtt":
        user, password = broker_credentials()
        sink = MqttSink(Site(tenant=args.tenant, site=args.site),
                        host=args.broker, port=args.broker_port,
                        username=user, password=password)
    else:
        sink = ConsoleSink(quiet=args.quiet)

    poller = Poller(gateways, sink, tcp_client_factory)

    total_devices = sum(len(g.devices) for g in gateways)
    total_points = sum(len(d.points) for g in gateways for d in g.devices)
    print(f"\npoller: {len(gateways)} gateway(s), {total_devices} device(s), "
          f"{total_points} point(s) -> {args.sink}")
    print("Ctrl+C to stop.\n")

    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    started = time.monotonic()
    poller.start()
    try:
        while not stopping.is_set():
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            stopping.wait(0.2)
    finally:
        print("\nstopping")
        poller.stop()
        print(f"  stats    {poller.stats}")
        if isinstance(sink, ConsoleSink):
            print(f"  readings {sink.count}")
        elif isinstance(sink, MqttSink):
            print(f"  published {sink.published}  queued {sink.queued}  "
                  f"failed {sink.failed}")
            sink.close()
        else:
            print(f"  written  {sink.written}   skipped {sink.skipped}")
            sink.close()


if __name__ == "__main__":
    main()
