"""
Register our PostgreSQL as a Windows service.

This is the first piece of installer code, and it closes the gap
setup_postgres.py names in its own docstring: "What it does NOT do,
because a dev machine does not need it: register Windows services,
create a service account, or set directory ACLs. That part comes with
the installer, and it is the remaining risk."

It stopped being hypothetical. The cluster was started from a shell, the
machine rebooted over a weekend, and on Monday the database was gone and
the dashboard had died behind it:

    psycopg.errors.ConnectionTimeout: connection timeout expired
      app.py:81 in partial_live -> queries.py:40 in connect
    [exited with code 4]

A service fixes the cause rather than the symptom. The service manager
owns the process, so no shell can take it down and a reboot brings it
back - which is also why start_detached() in setup_postgres.py calls
itself a development stand-in for this.

    python scripts/register_service.py              # register and start
    python scripts/register_service.py --status
    python scripts/register_service.py --unregister

MUST BE RUN AS ADMINISTRATOR. Creating a service writes to the service
control database, which an ordinary user cannot do. The script checks and
refuses rather than failing halfway.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# Not postgresql-x64-NN. That is the naming the EDB installer uses, and
# this machine already has a postgresql-x64-18 for the system PostgreSQL
# on 5432. Same reasoning as the ports: never collide with something a
# customer already has.
SERVICE = "jetpace-postgres"
DISPLAY = "Jetpace PostgreSQL (TimescaleDB)"
PORT = 5433

ROOT = Path(__file__).resolve().parent.parent
PGSQL = ROOT / "installer" / "vendor" / "pgsql"
PG_CTL = PGSQL / "bin" / "pg_ctl.exe"
DATA = ROOT / ".local" / "pgdata"

# NETWORK SERVICE is a built-in account with no password and few rights.
# LocalSystem - which is what pg_ctl register picks when you say nothing -
# has full control of the machine, so a flaw in the database process would
# be a flaw in everything. A database is exactly the kind of long-lived,
# network-listening process that should not run that way.
DEFAULT_ACCOUNT = r"NT AUTHORITY\NETWORK SERVICE"


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def need_admin() -> None:
    if is_admin():
        return
    die("this needs Administrator rights.\n\n"
        "  Press Start, type 'powershell', then Ctrl+Shift+Enter to open\n"
        "  it elevated. Then:\n\n"
        f'    cd "{ROOT}"\n'
        "    python scripts/register_service.py\n\n"
        "  Nothing has been changed.")


def service_exists() -> bool:
    return run(["sc", "query", SERVICE]).returncode == 0


def service_state() -> str:
    r = run(["sc", "query", SERVICE])
    if r.returncode != 0:
        return "not registered"
    for line in r.stdout.splitlines():
        if "STATE" in line:
            return line.split(":", 1)[1].strip()
    return "unknown"


def port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


# ---------------------------------------------------------------------------

def grant_access(account: str) -> None:
    """
    Let the service account reach and own the data directory.

    Two separate grants, and missing either one looks identical from the
    outside - the service start fails with a permissions error that names
    no path.

      The data directory itself needs full control, inherited by
      everything in it. PostgreSQL writes WAL, relation files and its own
      log there.

      Every directory ABOVE it needs traverse. A grant on pgdata is
      useless if the account cannot walk the path to get there, and this
      cluster sits inside a project folder rather than ProgramData, so
      that path is not one the account can already read. Traverse only -
      it does not let the account list or read those folders.
    """
    say(f"granting {account} access to the data directory")
    r = run(["icacls", str(DATA), "/grant", f"{account}:(OI)(CI)F", "/T",
             "/C", "/Q"])
    if r.returncode != 0:
        die(f"icacls failed on {DATA}:\n{r.stdout}\n{r.stderr}")

    for parent in (DATA.parent, DATA.parent.parent):      # .local, project
        r = run(["icacls", str(parent), "/grant", f"{account}:(X)", "/C",
                 "/Q"])
        if r.returncode != 0:
            say(f"  warning: could not grant traverse on {parent}")
        else:
            say(f"  traverse on {parent.name}")


def register(account: str) -> None:
    need_admin()
    if not PG_CTL.exists():
        die(f"{PG_CTL} is missing. Run scripts/setup_postgres.py first.")
    if not (DATA / "PG_VERSION").exists():
        die(f"{DATA} is not a PostgreSQL data directory. Run "
            f"scripts/setup_postgres.py first.")
    if service_exists():
        die(f"service {SERVICE} already exists ({service_state()}).\n\n"
            "  Remove it first:\n"
            "    python scripts/register_service.py --unregister")

    # A cluster started from a shell still holds the data directory and
    # the port. The service would fail to start against it, and the error
    # would be about a lock file rather than about this.
    say("stopping any shell-started cluster first")
    run([str(PG_CTL), "stop", "-D", str(DATA), "-m", "fast", "-w"],
        timeout=90)

    say(f"registering {SERVICE}")
    r = run([str(PG_CTL), "register",
             "-N", SERVICE,
             "-D", str(DATA),
             "-S", "auto"])            # auto: comes back after a reboot
    if r.returncode != 0:
        die(f"pg_ctl register failed:\n{r.stdout}\n{r.stderr}")

    grant_access(account)

    # pg_ctl register has -U/-P for this, but setting the account
    # afterwards with sc is explicit and reports its own failure. An empty
    # password is correct for a built-in account - it has none.
    say(f"setting the service to run as {account}")
    r = run(["sc", "config", SERVICE, "obj=", account, "password=", ""])
    if r.returncode != 0:
        die(f"could not set the service account:\n{r.stdout}\n{r.stderr}")

    run(["sc", "config", SERVICE, "DisplayName=", DISPLAY])
    run(["sc", "description", SERVICE,
         "PostgreSQL 17 with TimescaleDB, on port 5433, for Jetpace. "
         "Private to this install; not the system PostgreSQL."])

    # Restart on crash, with a backoff, and forget the count after a day.
    # This is the service manager doing a piece of the supervisor's job -
    # worth having now, and it does not replace the supervisor, which also
    # has to health-check and roll back.
    say("set to restart on failure (5s, 10s, then 30s)")
    run(["sc", "failure", SERVICE, "reset=", "86400",
         "actions=", "restart/5000/restart/10000/restart/30000"])

    start(account)


def start(account: str = DEFAULT_ACCOUNT) -> None:
    say("starting the service")
    r = run(["sc", "start", SERVICE], timeout=120)
    if r.returncode != 0:
        say(f"sc start returned {r.returncode}")

    for _ in range(60):
        time.sleep(0.5)
        if port_open():
            say(f"listening on 127.0.0.1:{PORT}")
            verify()
            return

    # The most likely cause by far, and the one with a one-line fix.
    print(f"""
ERROR: the service was registered but did not start.

  State: {service_state()}

  The usual cause is that {account} cannot reach
  {DATA}
  because a folder above it denies traverse. The cluster itself is fine -
  nothing in the database has been touched.

  Two ways on:

  1. Look at what PostgreSQL said. The reason is in the newest file in
     {DATA / 'log'}

  2. Or fall back to LocalSystem, which always has access. It is more
     privileged than a database should be, so this is a workaround and
     not the answer:

       sc config {SERVICE} obj= LocalSystem password= ""
       sc start {SERVICE}

  Meanwhile the old way still works:
     python scripts/setup_postgres.py --start
""", file=sys.stderr)
    sys.exit(1)


def verify(deadline: float = 60.0) -> None:
    """
    Prove it is the right cluster, not just something on the port.

    An open port is not a ready database. PostgreSQL binds its listening
    socket early and refuses connections until recovery finishes:

        FATAL:  the database system is starting up

    The first version of this checked port_open() and queried immediately,
    which on a cold start reported "could not query the cluster" against a
    cluster that was perfectly healthy two seconds later. So wait for the
    answer rather than for the socket.

    Each column is selected separately, too. Concatenating them meant one
    NULL - an extension not loaded, say - turned the whole row into NULL,
    printed as an empty line, and was reported as a failed query instead
    of as the missing extension it actually was.
    """
    psql = PGSQL / "bin" / "psql.exe"
    secret = ROOT / ".local" / "pg_password"
    if not (psql.exists() and secret.exists()):
        return
    env = {**os.environ, "PGPASSWORD": secret.read_text().strip()}
    sql = ("SELECT current_setting('server_version'), "
           "coalesce((SELECT extversion FROM pg_extension "
           "          WHERE extname='timescaledb'), 'NOT LOADED'), "
           "(SELECT count(*) FROM reading)")

    give_up = time.monotonic() + deadline
    last = ""
    while time.monotonic() < give_up:
        r = subprocess.run(
            [str(psql), "-h", "127.0.0.1", "-p", str(PORT),
             "-U", "jetpace_super", "-d", "jetpace", "-At", "-F", " | ",
             "-c", sql],
            capture_output=True, text=True, env=env, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            version, ts_version, rows = r.stdout.strip().split(" | ")
            say(f"PostgreSQL {version} | timescaledb {ts_version} | "
                f"{rows} readings")
            if ts_version == "NOT LOADED":
                say("WARNING: timescaledb did not load - check "
                    "shared_preload_libraries in conf.d/jetpace.conf")
            return
        last = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        if "starting up" in last[0]:
            say("waiting for recovery to finish")
        time.sleep(1.0)

    say(f"could not query the cluster after {deadline:.0f}s; it is "
        f"listening but check the log")
    if last:
        say(f"last error: {last[0]}")


def unregister() -> None:
    need_admin()
    if not service_exists():
        return say(f"{SERVICE} is not registered")
    say("stopping the service")
    run(["sc", "stop", SERVICE], timeout=120)
    for _ in range(40):
        time.sleep(0.5)
        if not port_open():
            break
    r = run([str(PG_CTL), "unregister", "-N", SERVICE])
    if r.returncode != 0:
        # pg_ctl unregister can refuse while the SCM still holds a handle.
        r = run(["sc", "delete", SERVICE])
        if r.returncode != 0:
            die(f"could not remove the service:\n{r.stdout}\n{r.stderr}")
    say(f"{SERVICE} removed. The data directory was not touched.")


def status() -> None:
    print()
    say(f"service   {SERVICE}")
    say(f"state     {service_state()}")
    say(f"port      127.0.0.1:{PORT} {'open' if port_open() else 'closed'}")
    say(f"data      {DATA}")
    if service_exists():
        r = run(["sc", "qc", SERVICE])
        for line in r.stdout.splitlines():
            if any(k in line for k in ("START_TYPE", "SERVICE_START_NAME")):
                say(line.strip())
    print()


def main() -> None:
    if os.name != "nt":
        die("Windows only. On Linux this is a systemd unit instead.")

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unregister", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT,
                    help="service account; LocalSystem is the fallback")
    args = ap.parse_args()

    if args.status:
        return status()
    if args.unregister:
        return unregister()

    print(f"\nRegistering {SERVICE}\n")
    register(args.account)
    print(f"\nDone. It will now start on boot.\n")
    print(f"  check it    python scripts/register_service.py --status")
    print(f"  remove it   python scripts/register_service.py --unregister\n")


if __name__ == "__main__":
    main()
