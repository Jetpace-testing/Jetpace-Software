"""
Build the Windows installer.

    uv run python scripts/make_installer.py --version 1.0.0

Runs scripts/build.py first unless --no-build, then calls makensis over
installer/jetpace.nsi. Produces dist/Jetpace-Setup-<version>.exe.

WHY NOT CALL makensis DIRECTLY

Because the version has to agree in three places - the installer's own
metadata, version.json inside it, and what the updater later compares a
manifest against. If they disagree, a site either refuses an update it
should take or takes one it should not, and the symptom appears weeks
later at somebody else's premises. One command, one version argument.

It also checks the payload is actually there before spending two minutes
compressing: an installer built without the PostgreSQL binaries looks
fine, installs fine, and fails at provisioning time on a customer's PC.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NSI = ROOT / "installer" / "jetpace.nsi"
VENDOR = ROOT / "installer" / "vendor"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

MAKENSIS_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "NSIS" / "makensis.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "NSIS" / "makensis.exe",
]

# What the installer must carry. Each is checked before the build, with a
# sentence saying how to get it, because the alternative is a 200 MB
# installer that fails on a customer's PC.
REQUIRED = [
    (DIST / "app" / "jetpace.exe",
     "the frozen application", "uv run python scripts/build.py --version X.Y.Z"),
    (DIST / "supervisor" / "supervisor.exe",
     "the frozen supervisor", "uv run python scripts/build.py --version X.Y.Z"),
    (DIST / "setup" / "jetpace-setup.exe",
     "the provisioning binary", "uv run python scripts/build.py --version X.Y.Z"),
    (DIST / "version.json", "the version marker",
     "uv run python scripts/build.py --version X.Y.Z"),
    (DIST / "config", "the device configuration", "see build.py"),
    (DIST / "migrations", "the SQL migrations", "see build.py"),
    (VENDOR / "pgsql" / "bin" / "postgres.exe",
     "the PostgreSQL binaries", "python scripts/setup_postgres.py"),
    (VENDOR / "mosquitto" / "mosquitto.exe",
     "the Mosquitto binaries", "python scripts/setup_mosquitto.py"),
]


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def find_makensis() -> Path:
    for p in MAKENSIS_CANDIDATES:
        if p.exists():
            return p
    from shutil import which
    found = which("makensis")
    if found:
        return Path(found)
    die("makensis.exe not found. NSIS builds the installer.\n\n"
        "  Install it:\n"
        "    winget install --id NSIS.NSIS\n\n"
        "  Then reopen the terminal.")


def check_payload() -> int:
    total = 0
    missing = []
    for path, what, how in REQUIRED:
        if not path.exists():
            missing.append((path, what, how))
            continue
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob("*")
                       if f.is_file())
        else:
            size = path.stat().st_size
        total += size
    if missing:
        lines = "\n".join(
            f"    {what}\n      missing: {p}\n      build it: {how}"
            for p, what, how in missing)
        die(f"the installer payload is incomplete:\n\n{lines}")

    # Directories carry more than the one file checked above.
    for d in (DIST / "app", DIST / "supervisor", DIST / "setup",
              VENDOR / "pgsql", VENDOR / "mosquitto"):
        total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return total


def version_in_dist() -> str | None:
    import json
    try:
        return json.loads(
            (DIST / "version.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="x.y.z")
    ap.add_argument("--no-build", action="store_true",
                    help="use whatever is already in dist/")
    args = ap.parse_args()

    if os.name != "nt":
        die("the installer can only be built on Windows.")
    if not SEMVER.match(args.version):
        die(f"--version {args.version!r} is not x.y.z")
    if not NSI.exists():
        die(f"{NSI} is missing")

    makensis = find_makensis()
    say(f"makensis {makensis}")

    if not args.no_build:
        print()
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build.py"),
             "--version", args.version],
            cwd=str(ROOT), timeout=3600)
        if r.returncode != 0:
            die("the build failed; the installer was not made")

    print()
    raw = check_payload()
    say(f"payload {raw / 1e6:.0f} MB before compression")

    # The one check that catches a mis-tagged installer: what is inside
    # must say the same thing as what is on the box.
    inside = version_in_dist()
    if inside != args.version:
        die(f"dist/version.json says {inside!r} but the installer would be "
            f"built as {args.version!r}.\n\n"
            f"  Rebuild without --no-build, or build with --version "
            f"{inside}.")
    say(f"version agrees inside and out: {args.version}")

    print()
    say("compressing - this takes a couple of minutes")
    started = time.monotonic()
    r = subprocess.run(
        [str(makensis), f"/DVERSION={args.version}", str(NSI)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-25:])
        die(f"makensis failed:\n{tail}")

    out = DIST / f"Jetpace-Setup-{args.version}.exe"
    if not out.exists():
        die(f"makensis reported success but {out} does not exist")
    say(f"built {out.name}  ({out.stat().st_size / 1e6:.0f} MB)")

    print(f"\n{out}\n")
    print("  NOT SIGNED. An unsigned installer gets a SmartScreen warning")
    print("  that a plant IT department is right to refuse. Authenticode")
    print("  signing is the remaining step before this goes to a customer.")
    print(f"\n  built in {time.monotonic() - started:.0f}s\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
