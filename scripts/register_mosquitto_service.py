"""
Register our Mosquitto as a Windows service.

    python scripts/register_mosquitto_service.py
    python scripts/register_mosquitto_service.py --status
    python scripts/register_mosquitto_service.py --unregister

MUST BE RUN AS ADMINISTRATOR.

This is the last hand-started piece. setup_mosquitto.py starts the broker
as a detached process, which is fine at a desk and useless on a site: it
does not survive a reboot. On 13 October the machine restarted and the
broker did not come back, so the poller and the ingest consumer spent
sixteen hours crash-looping on

    ConnectionRefusedError: [WinError 10061]

while the dashboard carried on serving the previous day's numbers. Nothing
was broken; nothing was running either.

HOW MOSQUITTO WANTS TO BE A SERVICE

Not with `sc create`. The first version of this script did that, with
`mosquitto.exe run -c <conf>` as the command, and the service registered
and then refused to start without logging anything anywhere. Run by hand
the same command says why:

    Error: Unknown option 'run'.

Mosquitto registers its own service, with verbs that `mosquitto -h` does
not list but which plainly exist - unelevated they answer "Access is
denied" rather than "Unknown option":

    mosquitto install
    mosquitto uninstall

The service it registers runs `<exe> run`, which only the service control
manager may use, and it reads its configuration from a file BESIDE THE
EXECUTABLE rather than from a command line. As of Mosquitto 2.1 a second
instance is made by copying the executable to a new name and installing
that - which is exactly what is wanted here, because the installer's own
instance must keep existing on 1883.

So: our binary is copied to jetpace-mosquitto.exe, our configuration is
written beside it, and that executable installs itself.

THE VENDOR SERVICE ON 1883

Installing Mosquitto leaves a service called `mosquitto` running
"C:\\Program Files\\Mosquitto\\mosquitto.exe" run with the DEFAULT
configuration - port 1883, every interface, no authentication.

On this machine 1883 belongs to Titan EMS. Both can bind it without an
error, because Titan takes the wildcard address and Mosquitto takes
loopback, and Windows permits that overlap. But loopback is the more
specific match, so Mosquitto wins it: anything connecting to
127.0.0.1:1883 expecting Titan EMS reaches Mosquitto instead.

This script disables that service, which is what setup_mosquitto.py asked
for when it told you to run `sc config mosquitto start= disabled`. Pass
--keep-vendor-service to leave it alone.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Mosquitto names the service after the executable, so the executable has
# to be named after the service we want.
SERVICE = "jetpace-mosquitto"
DISPLAY = "Jetpace MQTT Broker (Mosquitto)"
DESCRIPTION = ("Mosquitto on port 1887, loopback only, for Jetpace. A second "
               "instance, separate from the Mosquitto service the installer "
               "registers on 1883.")
VENDOR_SERVICE = "mosquitto"
PORT = 1887

VENDOR_DIR = ROOT / "installer" / "vendor" / "mosquitto"
SOURCE_EXE = VENDOR_DIR / "mosquitto.exe"
OUR_EXE = VENDOR_DIR / f"{SERVICE}.exe"
OUR_CONF = VENDOR_DIR / f"{SERVICE}.conf"
GENERATED_CONF = ROOT / ".local" / "mosquitto" / "jetpace.conf"


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
        "    python scripts/register_mosquitto_service.py\n\n"
        "  Nothing has been changed.")


def service_exists(name: str = SERVICE) -> bool:
    return run(["sc", "query", name]).returncode == 0


def service_state(name: str = SERVICE) -> str:
    r = run(["sc", "query", name])
    if r.returncode != 0:
        return "not registered"
    for line in r.stdout.splitlines():
        if "STATE" in line:
            return line.split(":", 1)[1].strip()
    return "unknown"


def service_pid(name: str = SERVICE) -> int | None:
    """The process id the SCM has for a service, or None if it has none."""
    r = run(["sc", "queryex", name])
    for line in r.stdout.splitlines():
        if "PID" in line:
            try:
                pid = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None
            return pid or None          # the SCM reports 0 when not running
    return None


def port_open(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def who_has(port: int) -> list[str]:
    """Report, do not act. 1883 is another product's port."""
    out = []
    for line in run(["netstat", "-ano"]).stdout.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            parts = line.split()
            pid = parts[-1]
            t = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"])
            name = (t.stdout.strip().splitlines() or [""])[-1].split(",")[0]
            out.append(f"{parts[1]}  pid {pid}  {name.strip(chr(34))}")
    return out


# ---------------------------------------------------------------------------

def lay_down_our_instance() -> None:
    """
    A second Mosquitto instance is a copy of the executable under a new
    name, with its configuration beside it. Mosquitto 2.1 documents this;
    there is no way to hand a service instance a config path instead.
    """
    if not SOURCE_EXE.exists():
        die(f"{SOURCE_EXE} is missing.\n\n"
            "  Vendor the binaries first:\n"
            "    python scripts/setup_mosquitto.py")
    if not GENERATED_CONF.exists():
        die(f"{GENERATED_CONF} is missing.\n\n"
            "  Generate the config, password and ACL first:\n"
            "    python scripts/setup_mosquitto.py")

    shutil.copy2(SOURCE_EXE, OUR_EXE)
    say(f"copied the broker to {OUR_EXE.name}")

    # The generated config already carries absolute paths for the password
    # file, the ACL, persistence and the log, so it works from anywhere.
    conf = GENERATED_CONF.read_text(encoding="utf-8")
    OUR_CONF.write_text(
        "# Copied from .local/mosquitto/jetpace.conf by\n"
        "# scripts/register_mosquitto_service.py. Mosquitto's Windows\n"
        "# service reads its config from beside the executable, so this\n"
        "# copy is where the service actually looks. Edit the script, or\n"
        "# setup_mosquitto.py, not this file.\n\n" + conf,
        encoding="utf-8")
    say(f"wrote {OUR_CONF.name} beside it  (listener {PORT}, loopback only)")

    # Belt and braces: if this build looks for mosquitto.conf rather than a
    # name matching the executable, it finds the same settings either way.
    fallback = VENDOR_DIR / "mosquitto.conf"
    if not fallback.exists():
        shutil.copy2(OUR_CONF, fallback)
        say("also wrote mosquitto.conf beside it, in case this build looks "
            "for that name")


def quieten_vendor_service() -> None:
    """
    Stop and disable the service the Mosquitto installer registered.

    Disabled, not removed: the binaries are wanted, and uninstalling would
    take them with it. One command reverses it.
    """
    if not service_exists(VENDOR_SERVICE):
        return say(f"the vendor service {VENDOR_SERVICE!r} is not present")

    say(f"stopping and disabling {VENDOR_SERVICE!r} (it binds 1883, which "
        f"Titan EMS owns)")
    run(["sc", "stop", VENDOR_SERVICE], timeout=60)
    for _ in range(40):
        time.sleep(0.25)
        if "STOPPED" in service_state(VENDOR_SERVICE):
            break
    r = run(["sc", "config", VENDOR_SERVICE, "start=", "disabled"])
    if r.returncode != 0:
        say(f"warning: could not disable it: {r.stderr.strip()}")
        say("         it will bind 1883 again at the next reboot")
    else:
        say(f"{VENDOR_SERVICE!r} disabled; re-enable with "
            f"`sc.exe config {VENDOR_SERVICE} start= auto`")


def register(keep_vendor: bool, replace: bool) -> None:
    need_admin()

    if service_exists():
        if not replace:
            die(f"service {SERVICE} already exists ({service_state()}).\n\n"
                "  Replace it:\n"
                "    python scripts/register_mosquitto_service.py --replace\n"
                "  or remove it:\n"
                "    python scripts/register_mosquitto_service.py "
                "--unregister")
        say("removing the existing registration first")
        unregister(quiet=True)

    lay_down_our_instance()

    # A broker started from a shell still holds the port; the service would
    # fail to bind and the error would be about the socket, not about this.
    if port_open():
        say(f"stopping the shell-started broker on {PORT}")
        run([sys.executable, str(ROOT / "scripts" / "setup_mosquitto.py"),
             "--stop"], timeout=60)
        for _ in range(20):
            time.sleep(0.25)
            if not port_open():
                break

    if not keep_vendor:
        quieten_vendor_service()

    say(f"registering {SERVICE} with mosquitto's own installer")
    r = run([str(OUR_EXE), "install"], timeout=120)
    if r.returncode != 0 or "denied" in (r.stdout + r.stderr).lower():
        die(f"`{OUR_EXE.name} install` failed:\n{r.stdout}\n{r.stderr}")
    if not service_exists():
        die(f"`{OUR_EXE.name} install` reported no error but no service "
            f"named {SERVICE} appeared.\n\n"
            f"  Look for a differently-named service:\n"
            f"    sc.exe query state= all | findstr /i mosquitto")

    # Mosquitto installs on demand; the whole point here is at boot. And
    # the failure actions matter: the broker is the one thing both the
    # poller and the ingest consumer depend on.
    run(["sc", "config", SERVICE, "start=", "auto"])
    run(["sc", "config", SERVICE, "DisplayName=", DISPLAY])
    run(["sc", "description", SERVICE, DESCRIPTION])
    say("set to start automatically")
    say("set to restart on failure (5s, 10s, then 30s)")
    r = run(["sc", "failure", SERVICE, "reset=", "86400",
             "actions=", "restart/5000/restart/10000/restart/30000"])
    if r.returncode != 0:
        say(f"warning: could not set failure actions: {r.stderr.strip()}")

    start()


def start() -> None:
    say("starting the service")
    r = run(["sc", "start", SERVICE], timeout=120)
    if r.returncode != 0:
        say(f"sc start returned {r.returncode}")

    for _ in range(60):
        time.sleep(0.5)
        if port_open():
            say(f"listening on 127.0.0.1:{PORT}")
            return
        if "STOPPED" in service_state():
            break

    log = ROOT / ".local" / "logs" / "mosquitto.log"
    print(f"""
ERROR: the service did not come up on {PORT}.

  State: {service_state()}

  Mosquitto writes its reason to
  {log}

  The configuration it reads is
  {OUR_CONF}
  and it can be checked on its own with
  "{OUR_EXE}" --test-config -c "{OUR_CONF}"

  Nothing else was changed, and starting it by hand still works:
     python scripts/setup_mosquitto.py --start

  To remove the service:
     python scripts/register_mosquitto_service.py --unregister
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
    for _ in range(40):
        time.sleep(0.5)
        if "STOPPED" in service_state():
            break
    else:
        # A service stuck in START_PENDING never reaches STOPPED, because
        # `sc stop` asks a process that is not listening to requests yet.
        # Waiting longer does not help - the process has to be killed.
        #
        # Which is what happened here: the hung jetpace-mosquitto was left
        # registered because the first version of this function waited for
        # a state that could never arrive, so the delete below never ran.
        state = service_state()
        say(f"it is {state} and will not stop; killing its process")
        # By PID from the service itself, never by image name. The
        # supervisor runs the live broker as mosquitto.exe, so
        # `taskkill /IM mosquitto.exe` would take down the working one
        # along with the hung one.
        pid = service_pid()
        if pid:
            r = run(["taskkill", "/F", "/PID", str(pid), "/T"])
            say(f"  pid {pid}: {'killed' if r.returncode == 0 else 'would not die'}")
        else:
            say("  could not find its process id; delete may fail")
        time.sleep(1.0)

    # Prefer mosquitto's own uninstall, so whatever its install created is
    # undone the way it expects. Fall back to sc delete for a service this
    # script's earlier, wrong version created with sc create.
    removed = False
    if OUR_EXE.exists():
        r = run([str(OUR_EXE), "uninstall"], timeout=120)
        removed = r.returncode == 0 and not service_exists()
    if not removed:
        r = run(["sc", "delete", SERVICE])
        if r.returncode != 0 and service_exists():
            die(f"could not remove the service:\n{r.stdout}\n{r.stderr}")

    # sc delete returns before the SCM has finished forgetting the name.
    for _ in range(40):
        if not service_exists():
            break
        time.sleep(0.25)
    say(f"{SERVICE} removed. The config and password were not touched.")


def status() -> None:
    print()
    say(f"service   {SERVICE}")
    say(f"state     {service_state()}")
    say(f"port      127.0.0.1:{PORT} {'open' if port_open() else 'closed'}")
    say(f"exe       {OUR_EXE}{'' if OUR_EXE.exists() else '   (missing)'}")
    say(f"config    {OUR_CONF}{'' if OUR_CONF.exists() else '   (missing)'}")
    if service_exists():
        for line in run(["sc", "qc", SERVICE]).stdout.splitlines():
            if any(k in line for k in ("START_TYPE", "SERVICE_START_NAME",
                                       "BINARY_PATH_NAME")):
                say(line.strip())
        if "RESTART" in run(["sc", "qfailure", SERVICE]).stdout.upper():
            say("failure   restart actions are set")
        else:
            say("failure   NO restart actions")
    print()
    say(f"vendor service {VENDOR_SERVICE!r}: {service_state(VENDOR_SERVICE)}")
    for line in run(["sc", "qc", VENDOR_SERVICE]).stdout.splitlines():
        if "START_TYPE" in line:
            say(f"  {line.strip()}")
    for port in (1883, PORT):
        holders = who_has(port)
        if holders:
            say(f"port {port} is held by:")
            for h in holders:
                say(f"  {h}")
    print()


def main() -> None:
    if os.name != "nt":
        die("Windows only.")

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unregister", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--replace", action="store_true",
                    help="remove an existing registration and register again")
    ap.add_argument("--keep-vendor-service", action="store_true",
                    help="leave the installer's `mosquitto` service alone; "
                         "it will keep binding 1883")
    args = ap.parse_args()

    if args.status:
        return status()
    if args.unregister:
        return unregister()

    print(f"\nRegistering {SERVICE}\n")
    register(keep_vendor=args.keep_vendor_service, replace=args.replace)
    print(f"\nDone. The broker now starts at boot.\n")
    print(f"  check it    python scripts/register_mosquitto_service.py "
          f"--status")
    print(f"  remove it   python scripts/register_mosquitto_service.py "
          f"--unregister\n")


if __name__ == "__main__":
    main()
