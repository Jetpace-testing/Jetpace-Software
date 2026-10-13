"""
Register the supervisor as a Windows service.

    python scripts/register_supervisor_service.py
    python scripts/register_supervisor_service.py --status
    python scripts/register_supervisor_service.py --unregister

MUST BE RUN AS ADMINISTRATOR.

This is what makes the update mechanism real. The supervisor does not roll
back a failed release itself - it exits, and the service manager starting
it again is what counts the attempt. After three, the previous version is
restored. Without `sc failure` configured below, a release that will not
start just stays broken.

WHICH ACCOUNT, AND WHY NOT THE LEAST-PRIVILEGE ONE

jetpace-postgres runs as NT AUTHORITY\\NETWORK SERVICE, which is right: a
database needs its data directory and nothing else. The supervisor is
different. It needs the whole install tree - it renames app/, writes
update/staging/ and update/backup/, and spawns processes.

On a development machine the install tree is this repository, and it
contains .local/, which holds the release signing private key. Granting
NETWORK SERVICE full control of this folder would hand that key to an
account several other Windows services also run as. LocalSystem can
already read it, so using LocalSystem creates no new exposure, while
granting NETWORK SERVICE would.

A production install is the other way round: the application lives in its
own directory with no signing key in it, and should run as a dedicated
account with full control of exactly that directory. Pass --account to do
that. The default here is chosen for this machine, not for a customer's.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

SERVICE = "jetpace-supervisor"
DISPLAY = "Jetpace Supervisor"
DESCRIPTION = ("Runs the Jetpace acquisition, ingest and dashboard "
               "processes, keeps them running, and applies signed software "
               "updates.")
CONFIG_ENV = "JETPACE_SUPERVISOR_CONFIG"
CONFIG_FILE = ROOT / "supervisor.json"

# The database is a separate service and the children cannot work without
# it. Windows starts dependencies first, which removes a whole class of
# "the dashboard died at boot" from the picture.
DEPENDS_ON = "jetpace-postgres"

MOSQUITTO_EXE = ROOT / "installer" / "vendor" / "mosquitto" / "mosquitto.exe"
MOSQUITTO_CONF = ROOT / ".local" / "mosquitto" / "jetpace.conf"

VENV = ROOT / ".venv"
PYTHON = VENV / "Scripts" / "python.exe"
PYTHONSERVICE = VENV / "Lib" / "site-packages" / "win32" / "pythonservice.exe"
SERVICE_HOST = ROOT / "scripts" / "run_service.py"
SERVICE_CLASS = "jetpace_core.service.JetpaceSupervisorService"


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
        "  Press Start, type 'powershell', then Ctrl+Shift+Enter. Then:\n\n"
        f'    cd "{ROOT}"\n'
        "    python scripts/register_supervisor_service.py\n\n"
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


# ---------------------------------------------------------------------------

def write_config(port: int, with_sim: bool, sim_units: int) -> Path:
    """
    What the service will run, as a file a person can read.

    A service cannot be handed command-line arguments the way a shell can,
    so this has to be written down. In the install root rather than the
    registry, so whoever is standing at the PC can see it.
    """
    py = str(PYTHON if PYTHON.exists() else Path(sys.executable))
    children: list[dict] = []

    # The broker comes first, and it is a child rather than a service of its
    # own. Mosquitto's Windows service support resisted three attempts:
    # `sc create` with `run -c <conf>` is rejected ("Unknown option 'run'"),
    # and `mosquitto install` on a renamed copy registers a service that
    # starts, binds nothing, logs nothing and hangs in StartPending. The
    # plain command below is documented, appears in `mosquitto -h`, passes
    # --test-config and ran for two days.
    #
    # The supervisor is already a service that starts at boot and restarts
    # what dies, so the broker gets the properties that were wanted anyway,
    # through a mechanism that is proven rather than reverse-engineered.
    #
    # Two consequences worth knowing. A supervisor restart - which every
    # update performs - bounces the broker; `persistence true` in the config
    # is what makes that survivable, and is the reason it is not optional.
    # And the poller and ingest will fail their first connection while the
    # broker is still binding, then succeed on the 5-second retry. Noisy in
    # the log for one cycle, self-healing, and far better than a service
    # that hangs.
    if MOSQUITTO_EXE.exists() and MOSQUITTO_CONF.exists():
        children.append({
            "name": "broker",
            "args": [str(MOSQUITTO_EXE), "-c", str(MOSQUITTO_CONF)],
        })
    else:
        say(f"WARNING: {MOSQUITTO_EXE.name} or its config is missing, so the "
            f"broker is not in the list. Run scripts/setup_mosquitto.py")

    if with_sim:
        children.append({
            "name": "simulator",
            "args": [py, str(ROOT / "tools" / "sim" / "idp100_sim.py"),
                     "--units", str(sim_units)],
            "required": False,       # its death must not fail a health gate
        })

    children += [
        {"name": "poller",
         "args": [py, str(ROOT / "scripts" / "run_poller.py"),
                  "--sink", "mqtt"]},
        {"name": "ingest",
         "args": [py, str(ROOT / "scripts" / "run_ingest.py")]},
        {"name": "web",
         "args": [py, str(ROOT / "scripts" / "run_web.py"),
                  "--port", str(port)]},
    ]

    config = {
        "_comment": ("Written by scripts/register_supervisor_service.py. "
                     "Read by the jetpace-supervisor service and by "
                     "scripts/run_supervisor.py, so both run the same thing."),
        "root": str(ROOT),
        "health_url": f"http://127.0.0.1:{port}/healthz",
        "logs": str(ROOT / ".local" / "logs"),
        "children": children,
    }
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n",
                           encoding="utf-8")
    say(f"wrote {CONFIG_FILE.name}  ({len(children)} children)")
    return CONFIG_FILE


def set_service_environment(config: Path) -> None:
    """
    Give the service its config path and its import path.

    Windows reads HKLM\\SYSTEM\\CurrentControlSet\\Services\\<name>\\
    Environment (REG_MULTI_SZ) and adds those entries to the service's own
    environment. Service start arguments are not usable for this: they come
    from whoever called `sc start`, so they are absent when the SCM starts
    the service at boot - which is the only start that really matters.

    PYTHONPATH is here for the same reason. pythonservice.exe has to import
    jetpace_core.service, and in a source checkout that package is not
    installed anywhere Python would look by itself.
    """
    entries = f"{CONFIG_ENV}={config}\\0PYTHONPATH={ROOT / 'packages' / 'core' / 'src'}"
    key = f"HKLM\\SYSTEM\\CurrentControlSet\\Services\\{SERVICE}"
    r = run(["reg", "add", key, "/v", "Environment", "/t", "REG_MULTI_SZ",
             "/d", entries, "/f"])
    if r.returncode != 0:
        die(f"could not set the service environment:\n{r.stdout}\n{r.stderr}")
    say(f"{CONFIG_ENV} and PYTHONPATH set for the service")


def register(account: str, port: int, with_sim: bool, sim_units: int,
             replace: bool = False) -> None:
    need_admin()
    if not PYTHONSERVICE.exists():
        die(f"{PYTHONSERVICE} is missing.\n\n"
            "  pywin32 provides it. Install dependencies first:\n"
            "    uv sync")
    if service_exists():
        if not replace:
            die(f"service {SERVICE} already exists ({service_state()}).\n\n"
                "  Replace it:\n"
                "    python scripts/register_supervisor_service.py --replace\n"
                "  or remove it:\n"
                "    python scripts/register_supervisor_service.py "
                "--unregister")
        say("removing the existing registration first")
        unregister(quiet=True)

    config = write_config(port, with_sim, sim_units)

    # Registered through pywin32's own installer rather than `sc create`.
    #
    # The first version here used sc.exe and wrote the registry by hand,
    # and it did not work: pythonservice.exe looks for the class as the
    # DEFAULT value of a PythonClass SUBKEY -
    #
    #     HKLM\\System\\CurrentControlSet\\Services\\<name>\\PythonClass
    #
    # - and the hand-rolled version wrote named values under Parameters
    # instead. The service started, could not find the class, and died
    # before it could log anything, which left an empty event log and a
    # STOPPED service with no explanation.
    #
    # InstallService knows this, so it does it. sc.exe is still used below
    # for the one thing pywin32 does not do: failure actions.
    say(f"registering {SERVICE}")
    import win32service                                        # noqa: PLC0415
    import win32serviceutil                                    # noqa: PLC0415

    try:
        win32serviceutil.InstallService(
            pythonClassString=SERVICE_CLASS,
            serviceName=SERVICE,
            displayName=DISPLAY,
            startType=win32service.SERVICE_AUTO_START,
            serviceDeps=[DEPENDS_ON],
            userName=None if account == "LocalSystem" else account,
            password=None,
            description=DESCRIPTION,
            # The venv's own python.exe hosts the service, running
            # scripts/run_service.py. Not pythonservice.exe - see the
            # comment above and the docstring of run_service.py.
            exeName=str(PYTHON),
            exeArgs=f'"{SERVICE_HOST}"',
        )
    except Exception as exc:                                   # noqa: BLE001
        die(f"could not register the service: {exc}")

    set_service_environment(config)

    # THE IMPORTANT LINE. Restart on failure, with a backoff. The last
    # action repeats for every later failure, so a site keeps trying.
    #
    # This is what drives the rollback: three restarts after a failed
    # health gate, then prepare() restores the backup on the fourth start.
    say("set to restart on failure (5s, 10s, then 30s)")
    r = run(["sc", "failure", SERVICE, "reset=", "86400",
             "actions=", "restart/5000/restart/10000/restart/30000"])
    if r.returncode != 0:
        say(f"warning: could not set failure actions: {r.stderr.strip()}")
        say("         without them a failed release will NOT roll back")

    start()


def start() -> None:
    say("starting the service")
    r = run(["sc", "start", SERVICE], timeout=120)
    if r.returncode != 0:
        say(f"sc start returned {r.returncode}")

    for _ in range(60):
        time.sleep(0.5)
        state = service_state()
        if "RUNNING" in state:
            say("running")
            return
        if "STOPPED" in state:
            break

    print(f"""
ERROR: the service did not stay running.

  State: {service_state()}

  Look in the Windows event log, Application, for '{SERVICE}' - the
  service writes its reason there. The supervisor's own log is
  {ROOT / '.local' / 'logs'}

  Nothing in the database or the install folder has been changed, and
  running it by hand still works:
     uv run python scripts/run_supervisor.py

  To remove the service:
     python scripts/register_supervisor_service.py --unregister
""", file=sys.stderr)
    sys.exit(1)


def unregister(quiet: bool = False) -> None:
    if not quiet:
        need_admin()
    if not service_exists():
        if not quiet:
            say(f"{SERVICE} is not registered")
        return
    say("stopping the service")
    run(["sc", "stop", SERVICE], timeout=120)
    for _ in range(60):
        time.sleep(0.5)
        if "STOPPED" in service_state():
            break
    r = run(["sc", "delete", SERVICE])
    if r.returncode != 0:
        die(f"could not remove the service:\n{r.stdout}\n{r.stderr}")
    say(f"{SERVICE} removed. Nothing else was touched.")


def status() -> None:
    print()
    say(f"service   {SERVICE}")
    say(f"state     {service_state()}")
    say(f"config    {CONFIG_FILE}"
        f"{'' if CONFIG_FILE.exists() else '   (missing)'}")
    if service_exists():
        r = run(["sc", "qc", SERVICE])
        for line in r.stdout.splitlines():
            if any(k in line for k in ("START_TYPE", "SERVICE_START_NAME",
                                       "DEPENDENCIES", "BINARY_PATH_NAME")):
                say(line.strip())
        f = run(["sc", "qfailure", SERVICE])
        if "RESTART" in f.stdout.upper():
            say("failure   restart actions are set")
        else:
            say("failure   NO restart actions - a failed release will not "
                "roll back")
    print()


def main() -> None:
    if os.name != "nt":
        die("Windows only. On Linux this is a systemd unit instead.")

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unregister", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--port", type=int, default=1885)
    ap.add_argument("--with-sim", action="store_true",
                    help="also run the device simulator (development)")
    ap.add_argument("--sim-units", type=int, default=3)
    ap.add_argument("--replace", action="store_true",
                    help="remove an existing registration and register again")
    ap.add_argument("--account", default="LocalSystem",
                    help="service account; see the note at the top of this "
                         "file before changing it")
    args = ap.parse_args()

    if args.status:
        return status()
    if args.unregister:
        return unregister()

    print(f"\nRegistering {SERVICE}\n")
    register(args.account, args.port, args.with_sim, args.sim_units,
             replace=args.replace)
    print(f"\nDone. It will now start at boot, before anyone signs in.\n")
    print(f"  check it    python scripts/register_supervisor_service.py --status")
    print(f"  logs        {ROOT / '.local' / 'logs'}")
    print(f"  remove it   python scripts/register_supervisor_service.py "
          f"--unregister\n")


if __name__ == "__main__":
    main()
