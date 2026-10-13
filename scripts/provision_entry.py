"""
Provision an install. What the installer calls, and nothing else calls.

    jetpace-setup.exe --all --root "C:\\Program Files\\Jetpace"
    jetpace-setup.exe --record --root "C:\\Program Files\\Jetpace"

Frozen by scripts/build.py into setup\\jetpace-setup.exe, and invoked by
installer/jetpace.nsi after the files are laid down.

WHY A SEPARATE BINARY RATHER THAN A `jetpace.exe provision` SUBCOMMAND

Because architecture.md §3 says nothing in packages/ may import from
scripts/, and the provisioning logic lives in scripts/ - correctly, since
it is composition: it drives initdb, psql, sc.exe and icacls in a fixed
order. Making it a subcommand of the application would have meant either
moving 2,275 lines of proven provisioning into the package, or breaking
the import rule. A third frozen binary costs 30 MB and breaks neither.

It also means the installer does not depend on the application being able
to start, which matters: at the moment this runs there is no database for
the application to connect to.

NOTHING HERE REIMPLEMENTS ANYTHING. The initdb flags, pg_hba contents,
grant list, migration runner and password generation are called as
functions out of the scripts that have been running on a development
machine for a week. This file is an order of operations and a progress
report.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_OF_CHECKOUT = HERE.parent

# Both the package and the sibling scripts have to be importable. In a
# frozen bundle PyInstaller has already put them on the path; these lines
# are for running this file directly out of a checkout.
for p in (ROOT_OF_CHECKOUT / "packages" / "core" / "src", HERE):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


def step(n: int, total: int, msg: str) -> None:
    print(f"\n[{n}/{total}] {msg}", flush=True)


def fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nPROVISIONING FAILED: {msg}\n", file=sys.stderr, flush=True)
    sys.exit(1)


def provision_database() -> None:
    import setup_postgres as pg

    if not pg.PGSQL.is_dir():
        fail(f"{pg.PGSQL} is missing. The installer should have laid the "
             f"PostgreSQL binaries down before calling this.")

    major = pg.detect_pg_major()
    print(f"  PostgreSQL {major} at {pg.PGSQL}", flush=True)
    # Already vendored inside the package, so this reports and returns.
    pg.install_timescale(major)
    pg.initdb()
    pg.ensure_started()
    pg.create_database()
    pg.migrate()
    pg.grant_privileges()


def provision_broker() -> None:
    import setup_mosquitto as mq

    if not (mq.VENDOR / "mosquitto.exe").exists():
        fail(f"{mq.VENDOR} has no mosquitto.exe. The installer should have "
             f"laid the broker down before calling this.")
    # Generates the password and ACL and writes the config; it does not
    # start anything, because the supervisor owns the broker as a child.
    mq.write_config()


def register_services(port: int) -> None:
    import register_service as dbsvc
    import register_supervisor_service as supsvc

    if not dbsvc.service_exists():
        dbsvc.register(dbsvc.DEFAULT_ACCOUNT)
    else:
        print(f"  {dbsvc.SERVICE} already registered", flush=True)

    if not supsvc.service_exists():
        supsvc.register("LocalSystem", port, with_sim=False, sim_units=0)
    else:
        print(f"  {supsvc.SERVICE} already registered", flush=True)


def wait_for_dashboard(port: int, deadline: float = 120.0) -> bool:
    """
    An install is not finished when the service starts; it is finished
    when the dashboard answers. Same lesson as everywhere else here: an
    open port is not a ready service, and a started service is not a
    working one.
    """
    import urllib.error
    import urllib.request

    give_up = time.monotonic() + deadline
    while time.monotonic() < give_up:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
                if r.status == 200 and json.loads(
                        r.read().decode("utf-8")).get("ok") is True:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(2.0)
    return False


def write_record(root: Path, port: int) -> None:
    """
    The installation record.

    For the environmental product this is IQ evidence - what was
    installed, where, when, by whom, and the hashes to prove the files
    have not changed since. It is written on every install whether anyone
    has asked for it, because the one time it is wanted is after the fact.
    """
    from jetpace_core import paths

    def digest(path: Path) -> str:
        import hashlib
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    files = {}
    for rel in ("app/jetpace.exe", "supervisor/supervisor.exe",
                "version.json"):
        p = root / rel
        if p.exists():
            files[rel] = {"sha256": digest(p), "bytes": p.stat().st_size}

    record = {
        "product": "Jetpace",
        "version": paths.version(),
        "installed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "install_root": str(root),
        "installed_by": os.environ.get("USERNAME", "unknown"),
        "machine": socket.gethostname(),
        "windows": platform.platform(),
        "dashboard": f"http://127.0.0.1:{port}/",
        "files": files,
    }

    out = paths.data_dir() / "installation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {out}", flush=True)
    for rel, info in files.items():
        print(f"    {rel:30} {info['sha256'][:16]}...", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path,
                    help="install root; defaults to where this executable is")
    ap.add_argument("--port", type=int, default=1885,
                    help="dashboard port (default 1885, the product default)")
    ap.add_argument("--all", action="store_true",
                    help="database, broker, services, and verify")
    ap.add_argument("--database", action="store_true")
    ap.add_argument("--broker", action="store_true")
    ap.add_argument("--services", action="store_true")
    ap.add_argument("--record", action="store_true",
                    help="write the installation record only")
    args = ap.parse_args()

    # Everything downstream reads the root through jetpace_core.paths, and
    # paths honours JETPACE_ROOT. Setting it here means the scripts need no
    # argument threaded through them.
    if args.root:
        os.environ["JETPACE_ROOT"] = str(args.root.resolve())

    from jetpace_core import paths
    root = paths.install_root()

    if args.record:
        write_record(root, args.port)
        return 0

    todo = []
    if args.all or args.database:
        todo.append(("Setting up the database", provision_database))
    if args.all or args.broker:
        todo.append(("Configuring the MQTT broker", provision_broker))
    if args.all or args.services:
        todo.append(("Registering the Windows services",
                     lambda: register_services(args.port)))
    if not todo:
        ap.print_help()
        return 2

    print(f"\nProvisioning {paths.version()} in {root}")
    print(f"  data      {paths.data_dir()}")
    print(f"  config    {paths.config_dir()}")

    total = len(todo) + (1 if args.all else 0)
    for i, (label, fn) in enumerate(todo, 1):
        step(i, total, label)
        try:
            fn()
        except SystemExit as exc:
            # The scripts call die(), and fail() above raises SystemExit too
            # - in both cases the reason has already been printed. Say which
            # step it was and stop, rather than printing a second banner
            # under the first, which is what this did at the one moment
            # somebody is reading carefully.
            print(f"  ...while {label.lower()} (exit {exc.code})",
                  file=sys.stderr, flush=True)
            return 1
        except Exception as exc:                            # noqa: BLE001
            fail(f"{label.lower()}: {exc}")

    if args.all:
        step(total, total, "Waiting for the dashboard to answer")
        if wait_for_dashboard(args.port):
            print(f"  the dashboard is answering on "
                  f"http://127.0.0.1:{args.port}/", flush=True)
            write_record(root, args.port)
        else:
            fail(f"the services were registered but nothing answered on "
                 f"http://127.0.0.1:{args.port}/healthz within two minutes.\n"
                 f"  Look in {paths.logs_dir()} - the supervisor and each "
                 f"child write their own log there.")

    print("\nDone.\n", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
