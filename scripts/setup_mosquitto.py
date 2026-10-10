"""
Stand up a private Mosquitto for development.

Same shape as setup_postgres.py, and for the same reason: a private copy
on a non-default port, bound to loopback, with its own credentials - so it
cannot collide with anything else on the machine. On this one that is not
hypothetical: titan-service.exe already owns 1883.

    python scripts/setup_mosquitto.py          # vendor, configure, start
    python scripts/setup_mosquitto.py --start
    python scripts/setup_mosquitto.py --stop
    python scripts/setup_mosquitto.py --status

ONE MANUAL STEP FIRST. Mosquitto publishes no zip for Windows - all 51
builds on mosquitto.org are installers - so the binaries have to come from
one. Run, as Administrator:

    .local\\mosquitto-installer.exe /S
    sc stop mosquitto
    sc config mosquitto start= disabled

The second and third lines matter. The installer registers a service that
binds 1883, and on the next reboot it would race Titan EMS for that port.
Disabling it leaves the binaries, which is all we want - we run our own
instance, on our own port, from our own config.

This script then copies those binaries into installer/vendor/mosquitto so
the project owns a pinned copy, exactly as it does for PostgreSQL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

PORT = 1887                      # 1883 Titan EMS, 1884 a node service,
                                 # 1885 our own dashboard. Three
                                 # collisions on one laptop is why the
                                 # rule is "never the default", and why
                                 # the port is a setting, not a constant.
USER = "jetpace_app"

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "installer" / "vendor" / "mosquitto"
LOCK = ROOT / "installer" / "vendor" / "vendor.lock.json"
LOCAL = ROOT / ".local" / "mosquitto"
LOGS = ROOT / ".local" / "logs"
CONF = LOCAL / "jetpace.conf"
PASSWD = LOCAL / "passwd"
ACL = LOCAL / "acl"
SECRET = ROOT / ".local" / "mqtt_password"
PIDFILE = LOCAL / "mosquitto.pid"

# Where the installer puts things, in the order worth looking.
CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "mosquitto",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "mosquitto",
    VENDOR,
]

NEEDED = ["mosquitto.exe", "mosquitto_passwd.exe", "mosquitto_pub.exe",
          "mosquitto_sub.exe"]


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def find_install() -> Path:
    for base in CANDIDATES:
        if (base / "mosquitto.exe").exists():
            return base
    die(
        "mosquitto.exe not found.\n\n"
        "  Run this once, as Administrator:\n"
        "    .local\\mosquitto-installer.exe /S\n"
        "    sc stop mosquitto\n"
        "    sc config mosquitto start= disabled\n\n"
        "  Disabling the service matters: it binds 1883, which Titan EMS\n"
        "  already owns, and they would race on the next reboot."
    )


def vendor_binaries(src: Path) -> None:
    """Copy the binaries and their DLLs into the project's own vendor tree."""
    if (VENDOR / "mosquitto.exe").exists():
        say("mosquitto/ already vendored")
        return
    VENDOR.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in src.iterdir():
        if f.is_file() and f.suffix.lower() in (".exe", ".dll"):
            shutil.copy2(f, VENDOR / f.name)
            copied += 1
    missing = [n for n in NEEDED if not (VENDOR / n).exists()]
    if missing:
        die(f"vendored {copied} files but these are missing: {missing}")
    say(f"vendored {copied} files from {src}")

    digest = hashlib.sha256(
        (VENDOR / "mosquitto.exe").read_bytes()).hexdigest()
    lock = json.loads(LOCK.read_text()) if LOCK.exists() else {}
    lock.setdefault("mosquitto", {})["binary_sha256"] = digest
    LOCK.write_text(json.dumps(lock, indent=2) + "\n")


def exe(name: str) -> Path:
    path = VENDOR / name
    if not path.exists():
        die(f"{path} is missing; run setup without --start first")
    return path


def write_config() -> None:
    LOCAL.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    if not PASSWD.exists():
        password = secrets.token_urlsafe(24)
        PASSWD.write_text("")
        r = subprocess.run(
            [str(exe("mosquitto_passwd.exe")), "-b", str(PASSWD), USER, password],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            die(f"mosquitto_passwd failed:\n{r.stdout}\n{r.stderr}")
        SECRET.write_text(password)
        say(f"created broker user {USER}; password in "
            f"{SECRET.relative_to(ROOT)}")
    else:
        say("password file already present")

    # Tenant isolation lives in the ACL. One line per principal, scoped by
    # topic prefix - which is why the topic tree starts with the tenant.
    ACL.write_text(
        "# Per-principal topic scoping. A second tenant on this broker\n"
        "# would get its own block and could not see this one's traffic.\n"
        f"user {USER}\n"
        "topic readwrite jetpace/#\n"
    )

    def posix(p: Path) -> str:
        # Mosquitto accepts forward slashes on Windows and they avoid every
        # escaping question in a config file.
        return str(p).replace("\\", "/")

    CONF.write_text(f"""# written by scripts/setup_mosquitto.py - edit the script, not this file

listener {PORT} 127.0.0.1
allow_anonymous false
password_file {posix(PASSWD)}
acl_file {posix(ACL)}

# NOT OPTIONAL. Without persistence the broker holds queued and retained
# messages in memory only - so every retained device-status topic vanishes
# on restart, and the updater restarts services on every release. It looks
# exactly like data loss from an unrelated cause.
persistence true
persistence_location {posix(LOCAL)}/
autosave_interval 30

max_queued_messages 100000

log_dest file {posix(LOGS)}/mosquitto.log
log_type error
log_type warning
log_type notice
""")
    say(f"wrote {CONF.relative_to(ROOT)}  (listener {PORT}, loopback only)")


# ---------------------------------------------------------------------------

def is_running() -> bool:
    if not PIDFILE.exists():
        return False
    try:
        pid = int(PIDFILE.read_text().strip())
    except ValueError:
        return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                       capture_output=True, text=True, timeout=30)
    return str(pid) in r.stdout


def start() -> None:
    if is_running():
        say(f"already running on 127.0.0.1:{PORT}")
        return

    # Same two Windows problems as the database: capturing output from a
    # long-lived child hangs, and a job object kills anything inside it
    # when the launching shell exits. Hence DEVNULL and breakaway.
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    try:
        proc = subprocess.Popen(
            [str(exe("mosquitto.exe")), "-c", str(CONF)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        flags &= ~subprocess.CREATE_BREAKAWAY_FROM_JOB
        proc = subprocess.Popen(
            [str(exe("mosquitto.exe")), "-c", str(CONF)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags)

    PIDFILE.write_text(str(proc.pid))
    for _ in range(40):
        time.sleep(0.25)
        if proc.poll() is not None:
            die(f"mosquitto exited immediately; see {LOGS / 'mosquitto.log'}")
        if port_open():
            say(f"listening on 127.0.0.1:{PORT}  (pid {proc.pid})")
            return
    die(f"started but not listening on {PORT}; see {LOGS / 'mosquitto.log'}")


def port_open() -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def stop() -> None:
    if not PIDFILE.exists():
        return say("not running")
    pid = PIDFILE.read_text().strip()
    subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                   capture_output=True, text=True, timeout=30)
    PIDFILE.unlink(missing_ok=True)
    say(f"stopped (pid {pid})")


def status() -> None:
    running = is_running()
    say(f"{'running' if running else 'not running'}  "
        f"127.0.0.1:{PORT}  port {'open' if port_open() else 'closed'}")
    if SECRET.exists():
        print(f"\n  broker user  {USER}")
        print(f"  password     {SECRET.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        return status()
    if args.stop:
        return stop()
    if args.start:
        return start()

    print("\nJetpace development broker\n")
    vendor_binaries(find_install())
    write_config()
    start()
    print(f"\nReady.\n")
    print(f"  broker    127.0.0.1:{PORT}   user {USER}")
    print(f"  password  {SECRET.relative_to(ROOT)}")
    print(f"  stop it   python scripts/setup_mosquitto.py --stop\n")


if __name__ == "__main__":
    main()
