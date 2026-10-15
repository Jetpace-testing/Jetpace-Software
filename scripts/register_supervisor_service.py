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

def enabled_products() -> list[str]:
    """
    Which products this install serves, asked of the same code the
    dashboard asks.

    Not a list written here: this script is shared by both products and
    must name neither, and a second copy of the answer would drift from
    config/products.toml the first time somebody edited one of them.
    """
    try:
        from jetpace_core.web import products
    except ImportError:            # pragma: no cover - a broken checkout
        say("WARNING: jetpace_core is not importable, so no product "
            "dashboards will be configured")
        return []
    configured = products.enabled()
    if configured is None:
        return [s for s in products.KNOWN if products.importable(s)]
    return list(configured)


def port_is_free(port: int) -> bool:
    """
    Whether we can bind it right now.

    Worth asking before writing a config: the ports a product dashboard
    lands on are derived from the base port, and on a developer machine
    the obvious derivations collide with other projects. A supervisor
    child that cannot bind exits, gets restarted, exits again - visible
    only in its own log.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def write_config(port: int, with_sim: bool, sim_units: int,
                 product_host: str = "127.0.0.1",
                 product_ports: dict[str, int] | None = None) -> Path:
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

    # --devices db, not the gateway sheet. The device list comes from the
    # device and gateway tables and is re-read every few seconds, which is
    # what makes an instrument commissioned from the dashboard start being
    # polled without anybody restarting this service (debt #3,
    # architecture.md §10 step 4).
    #
    # The file path still exists and is still the CLI default, because a
    # developer wants a checkout to behave the same way twice and because
    # config/prototype.toml is what seed_prototype.py reads. An INSTALL
    # wants the database, because an install has an operator and no text
    # editor.
    #
    # The reload interval is spelled out rather than left to default so
    # that it reads against web/app.py's DEVICE_RELOAD_SECONDS, which is
    # the number the commissioning form promises an operator. Nothing can
    # discover it across the two processes; if one changes, change both.
    children += [
        {"name": "poller",
         "args": [py, str(ROOT / "scripts" / "run_poller.py"),
                  "--sink", "mqtt", "--devices", "db", "--reload", "10"]},
        {"name": "ingest",
         "args": [py, str(ROOT / "scripts" / "run_ingest.py")]},
    ]

    # The administrative dashboard: every product this install carries, so
    # whoever is looking after the system can see all of it, and bound to
    # 127.0.0.1 so that only someone at this PC can reach it. That bind is
    # the one access boundary here that is actually enforced - by the
    # operating system, which refuses the connection - because nothing in
    # this software authenticates anybody yet (§10).
    children.append(
        {"name": "web",
         "args": [py, str(ROOT / "scripts" / "run_web.py"),
                  "--host", "127.0.0.1", "--port", str(port)]})

    # Then one dashboard per product, each carrying only its own views, on
    # its own port, for the people who use that product. A separate port
    # is a separate URL and NOT a separate permission: anyone who can
    # reach one of these can type the administrative port unless a
    # firewall or a different --product-host stops them.
    slugs = enabled_products()
    ports = dict(product_ports or {})
    for index, slug in enumerate(slugs):
        chosen = ports.get(slug, port + 1 + index)
        ports[slug] = chosen
        children.append(
            {"name": f"web-{slug}",
             "args": [py, str(ROOT / "scripts" / "run_web.py"),
                      "--host", product_host,
                      "--port", str(chosen),
                      "--products", slug]})

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

    # Say exactly what was written, every time.
    #
    # This block exists because of a real incident: this script was run
    # with --replace and nothing else, and its defaults quietly moved the
    # dashboard to another port and dropped the simulator. Both are
    # one-word flags and both failures were silent - the service came
    # back up, /healthz answered ok:true because it counted readings over
    # 24 hours rather than checking freshness, and the data had stopped.
    # A config written without being read back is a config nobody
    # checked. (/healthz checks freshness now; see architecture.md §5.)
    say(f"wrote {CONFIG_FILE.name}  ({len(children)} children)")
    print()
    for child in children:
        detail = ""
        if child["name"].startswith("web"):
            argv = child["args"]
            host = argv[argv.index("--host") + 1]
            bound = argv[argv.index("--port") + 1]
            where = ("this machine only" if host == "127.0.0.1"
                     else f"reachable on {host}")
            detail = f"http://{host}:{bound}/   {where}"
        optional = "" if child.get("required", True) else "  (optional)"
        print(f"    {child['name']:16} {detail}{optional}", flush=True)
    print()

    if not with_sim:
        say("no simulator: the poller will read real instruments only. "
            "Pass --with-sim if this machine has none.")

    taken = [(c["name"], c["args"][c["args"].index("--port") + 1])
             for c in children if c["name"].startswith("web")]
    busy = [f"{name} on {bound}" for name, bound in taken
            if not port_is_free(int(bound))]
    if busy:
        say(f"NOTE: already in use: {', '.join(busy)}.")
        say("  A port held by the dashboard being replaced is expected - the "
            "old child is stopped before the new one binds. A port held by "
            "anything else means that dashboard will fail to bind and be "
            "restarted in a loop, visible only in its own log; pass --port "
            "or --product-port to move it.")

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
    # Every workspace member, not just core. One directory was right while
    # core was the only package; it stops being right as soon as the web
    # child imports a product, and the symptom is a service that dies at
    # boot with nothing in the event log. Globbed so a third product needs
    # no edit, and so this shared script names neither product.
    #
    # Two separators in one string, and mixing them up would quietly
    # create a bogus third registry entry: `\0` is REG_MULTI_SZ's entry
    # separator, which `reg add` parses, while os.pathsep (';') is what
    # Python splits PYTHONPATH on.
    sources = os.pathsep.join(
        str(p) for p in sorted((ROOT / "packages").glob("*/src")))
    entries = f"{CONFIG_ENV}={config}\\0PYTHONPATH={sources}"
    key = f"HKLM\\SYSTEM\\CurrentControlSet\\Services\\{SERVICE}"
    r = run(["reg", "add", key, "/v", "Environment", "/t", "REG_MULTI_SZ",
             "/d", entries, "/f"])
    if r.returncode != 0:
        die(f"could not set the service environment:\n{r.stdout}\n{r.stderr}")
    say(f"{CONFIG_ENV} and PYTHONPATH set for the service")


def register(account: str, port: int, with_sim: bool, sim_units: int,
             replace: bool = False, product_host: str = "127.0.0.1",
             product_ports: dict[str, int] | None = None) -> None:
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

    config = write_config(port, with_sim, sim_units,
                          product_host=product_host,
                          product_ports=product_ports)

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
    ap.add_argument("--config-only", action="store_true",
                    help="rewrite supervisor.json and nothing else. The "
                         "child list is a FILE the service reads at start; "
                         "the registry holds only the service account, the "
                         "executable and PYTHONPATH. Changing which "
                         "arguments the poller gets therefore needs a "
                         "restart, not a re-registration - and a "
                         "re-registration is the one operation here that "
                         "can take the stack down")
    ap.add_argument("--port", type=int, default=1885)
    ap.add_argument("--with-sim", action="store_true",
                    help="also run the device simulator (development)")
    ap.add_argument("--sim-units", type=int, default=3)
    ap.add_argument("--replace", action="store_true",
                    help="remove an existing registration and register again")
    ap.add_argument("--product-host", default="127.0.0.1",
                    help="interface for the per-product dashboards. "
                         "127.0.0.1 keeps them on this machine; a site "
                         "that wants them on the LAN passes 0.0.0.0 - and "
                         "should know that nothing authenticates anybody "
                         "yet")
    ap.add_argument("--product-port", action="append", default=[],
                    metavar="SLUG=PORT",
                    help="pin one product's dashboard port. Repeatable. "
                         "Without it, ports are derived from --port, which "
                         "on a machine running other projects may collide")
    ap.add_argument("--account", default="LocalSystem",
                    help="service account; see the note at the top of this "
                         "file before changing it")
    args = ap.parse_args()

    if args.status:
        return status()
    if args.unregister:
        return unregister()

    pinned: dict[str, int] = {}
    for item in args.product_port:
        slug, _, value = item.partition("=")
        if not value.isdigit():
            die(f"--product-port {item!r} is not SLUG=PORT")
        pinned[slug.strip()] = int(value)

    if args.config_only:
        print(f"\nRewriting {CONFIG_FILE.name}\n")
        write_config(args.port, args.with_sim, args.sim_units,
                     product_host=args.product_host, product_ports=pinned)
        # It is read at start, so nothing has changed yet. Saying so is
        # the point: a config written and not applied looks exactly like
        # a config that was applied.
        print(f"  Not applied yet. In an elevated shell:\n")
        print(f"    Restart-Service {SERVICE}\n")
        return

    print(f"\nRegistering {SERVICE}\n")
    register(args.account, args.port, args.with_sim, args.sim_units,
             replace=args.replace, product_host=args.product_host,
             product_ports=pinned)
    print(f"\nDone. It will now start at boot, before anyone signs in.\n")
    print(f"  check it    python scripts/register_supervisor_service.py --status")
    print(f"  logs        {ROOT / '.local' / 'logs'}")
    print(f"  remove it   python scripts/register_supervisor_service.py "
          f"--unregister\n")


if __name__ == "__main__":
    main()
