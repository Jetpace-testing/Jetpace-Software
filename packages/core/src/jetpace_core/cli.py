"""
One executable, three jobs: `jetpace poller`, `jetpace ingest`,
`jetpace web`.

A frozen install ships one application executable rather than three,
because three PyInstaller bundles would mean three copies of Python,
psycopg, pymodbus and uvicorn - a few hundred megabytes to say the same
thing three times. The supervisor starts `app\\jetpace.exe poller` and so
on, which is why its child list is a list of argument vectors rather than
of executables.

The wiring here came out of scripts/run_poller.py, run_ingest.py and
run_web.py. Those remain as thin wrappers so that every command written
down in the last week still works, but the logic lives in the package,
where PyInstaller can reach it.
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

from . import paths
from .ingest import Ingest
from .modbus_client import tcp_client_factory
from .mqtt import Site
from .poller import DeviceSpec, GatewaySpec, PointSpec, Poller
from .sinks import ConsoleSink, MqttSink, PostgresSink


# --- configuration --------------------------------------------------------

def load_config(path: Path) -> list[GatewaySpec]:
    """
    Read the gateway and device list from TOML.

    Still a file rather than the database - see architecture.md §6, which
    says it belongs in a table and explains why moving it needs a dry-run
    diff rather than a silent reconcile.
    """
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            f"{path} is missing, so there are no devices to poll.") from None
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{path} is not valid TOML: {exc}") from None

    # Points are declared once per [model.<name>] and shared by every
    # device of that model, so a register map is written down in one place
    # and a datasheet correction is one edit rather than four hundred.
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
                raise SystemExit(
                    f"device {d['code']}: no [model.{model}] section")
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
    if not gateways:
        raise SystemExit(f"{path} describes no gateways.")
    return gateways


# --- poller ---------------------------------------------------------------

def cmd_poller(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    gateways = load_config(args.config or paths.config_dir() / "prototype.toml")

    # The whole point of the Sink Protocol: three implementations, one
    # construction site, and nothing below this line knows the difference.
    if args.sink == "postgres":
        sink = PostgresSink(paths.postgres_dsn())
    elif args.sink == "mqtt":
        user, password = paths.broker_credentials()
        sink = MqttSink(Site(tenant=args.tenant, site=args.site),
                        host=args.broker, port=args.broker_port,
                        username=user, password=password)
    else:
        sink = ConsoleSink(quiet=args.quiet)

    poller = Poller(gateways, sink, tcp_client_factory)
    devices = sum(len(g.devices) for g in gateways)
    points = sum(len(d.points) for g in gateways for d in g.devices)
    print(f"\npoller: {len(gateways)} gateway(s), {devices} device(s), "
          f"{points} point(s) -> {args.sink}", flush=True)

    stopping = _stop_on_signal()
    started = time.monotonic()
    poller.start()
    try:
        while not stopping.is_set():
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            stopping.wait(0.2)
    finally:
        print("\nstopping", flush=True)
        poller.stop()
        print(f"  stats    {poller.stats}", flush=True)
        if isinstance(sink, ConsoleSink):
            print(f"  readings {sink.count}", flush=True)
        elif isinstance(sink, MqttSink):
            print(f"  published {sink.published}  queued {sink.queued}  "
                  f"failed {sink.failed}", flush=True)
            sink.close()
        else:
            print(f"  written  {sink.written}   skipped {sink.skipped}",
                  flush=True)
            sink.close()
    return 0


# --- ingest ---------------------------------------------------------------

def cmd_ingest(args: argparse.Namespace) -> int:
    import paho.mqtt.client as paho

    site = Site(tenant=args.tenant, site=args.site)
    sink = PostgresSink(paths.postgres_dsn())
    ingest = Ingest(site, sink)

    # The client id must be unique per process. A hardcoded one made two
    # pollers evict each other 28 times in 25 seconds.
    client = paho.Client(paho.CallbackAPIVersion.VERSION2,
                         client_id=f"jetpace-ingest-{_pid()}",
                         protocol=paho.MQTTv5)
    user, password = paths.broker_credentials()
    if user:
        client.username_pw_set(user, password)
    client.on_connect = ingest.on_connect
    client.on_message = ingest.on_message

    print(f"\ningest: {args.broker}:{args.broker_port} -> postgres",
          flush=True)
    client.connect(args.broker, args.broker_port, keepalive=30)
    client.loop_start()

    writer = threading.Thread(target=ingest.drain, name="ingest-writer",
                              daemon=True)
    writer.start()

    stopping = _stop_on_signal()
    started = time.monotonic()
    try:
        while not stopping.is_set():
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            stopping.wait(0.2)
    finally:
        print("\nstopping", flush=True)
        ingest.stopping.set()
        writer.join(timeout=10)
        client.loop_stop()
        client.disconnect()
        print(f"  received {ingest.received}  rejected {ingest.rejected}  "
              f"status changes {ingest.status_changes}", flush=True)
        print(f"  written  {sink.written}   skipped {sink.skipped}",
              flush=True)
        sink.close()
    return 0


# --- web ------------------------------------------------------------------

def cmd_web(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app
    from .web.products import load_default

    # Which products this install serves, from config/products.toml or
    # from whatever is installed. Printed below because a missing product
    # is otherwise invisible - the dashboard comes up, answers /healthz
    # and is simply missing a tab.
    enabled = load_default()
    app = create_app(paths.postgres_dsn(), enabled)
    print(f"\n  dashboard   http://{args.host}:{args.port}/", flush=True)
    print(f"  products    "
          f"{', '.join(p.slug for p in enabled) or 'none'}", flush=True)
    print(f"  health      http://{args.host}:{args.port}/healthz", flush=True)
    print(f"  api docs    http://{args.host}:{args.port}/api/docs\n",
          flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# --- where am I -----------------------------------------------------------

def cmd_where(args: argparse.Namespace) -> int:
    """
    Print the resolved paths. The first thing to run on a site when
    something cannot find a file, because it answers "which install is
    this and where does it think its data lives" in one line each.
    """
    print()
    print(f"  version      {paths.version()}")
    print(f"  frozen       {paths.is_frozen()}")
    print(f"  executable   {sys.executable}")
    print(f"  install root {paths.install_root()}")
    print(f"  data         {paths.data_dir()}")
    print(f"  config       {paths.config_dir()}")
    print(f"  logs         {paths.logs_dir()}")
    print()
    for name in ("app_password", "mqtt_password", "pg_password"):
        p = paths.data_dir() / name
        print(f"  {name:16} {'present' if p.exists() else 'MISSING'}")

    # Which products this install can actually serve. Printed here rather
    # than in a subcommand of its own so that the build's existing smoke
    # test - which already runs `jetpace where` against the frozen
    # executable - can assert on it. A product reached by
    # importlib.import_module is invisible to PyInstaller's analysis, so
    # the failure this guards against is a frozen app that starts
    # healthily and silently serves no product pages.
    from .web import products as products_
    print()
    configured = products_.enabled()
    print(f"  products     {'from config' if configured else 'whatever is installed'}")
    for slug in (configured if configured else products_.KNOWN):
        ok = products_.importable(slug)
        print(f"  {slug:16} {'present' if ok else 'MISSING'}")
    print()
    return 0


# --- plumbing -------------------------------------------------------------

def _pid() -> int:
    import os
    return os.getpid()


def _stop_on_signal() -> threading.Event:
    stopping = threading.Event()

    def handler(signum, _frame):
        stopping.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass        # not the main thread, or not supported here
    return stopping


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="jetpace",
        description="Jetpace acquisition, ingest and dashboard.")
    ap.add_argument("--version", action="store_true",
                    help="print the installed version and exit")
    sub = ap.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--tenant", default="jetpace")
        p.add_argument("--site", default="prototype")
        p.add_argument("--broker", default="127.0.0.1")
        p.add_argument("--broker-port", type=int, default=1887)
        p.add_argument("--seconds", type=float,
                       help="stop after this long (for tests)")

    p = sub.add_parser("poller", help="read the instruments")
    p.add_argument("--config", type=Path)
    p.add_argument("--sink", choices=("console", "postgres", "mqtt"),
                   default="console")
    p.add_argument("--quiet", action="store_true")
    common(p)
    p.set_defaults(func=cmd_poller)

    p = sub.add_parser("ingest", help="broker to historian")
    common(p)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("web", help="serve the dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1885)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("where", help="print the resolved paths")
    p.set_defaults(func=cmd_where)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.version:
        print(paths.version())
        return 0
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
