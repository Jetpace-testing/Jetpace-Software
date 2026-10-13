"""
Freeze the application and the supervisor into an installable layout.

    uv run python scripts/build.py --version 1.0.0
    uv run python scripts/build.py --version 1.0.0 --app-only

Produces, under dist/:

    dist/
      app/                   the application; jetpace.exe + _internal/
      supervisor/            the supervisor; its own bundle
      version.json
      config/
      migrations/

That layout is the point. `app/` is what a release replaces, which is why
nothing else lives in it: not the supervisor (a running executable holds
its own file open and cannot replace itself), not version.json's parent,
not the data directory, and not the database.

--onedir, never --onefile. One-file re-extracts the whole bundle to a temp
directory on every start, which antivirus software treats as suspicious
and which makes startup slow for no benefit - and the updater swaps a
DIRECTORY, so a single file could not be swapped the same way.

ONE EXECUTABLE, THREE JOBS. The application is frozen once and dispatches
on its first argument - `jetpace.exe poller`, `ingest`, `web`. Three
separate bundles would be three copies of Python, psycopg, pymodbus,
uvicorn and fastapi: several hundred megabytes to say the same thing
three times.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "packages" / "core" / "src"
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"

# A launcher outside the package, not cli.py itself. PyInstaller freezes a
# script, and a script runs as __main__ with no parent package - so freezing
# cli.py directly produced an executable that died on `from . import paths`
# with "attempted relative import with no known parent package".
APP_ENTRY = ROOT / "scripts" / "app_entry.py"
SUPERVISOR_ENTRY = ROOT / "scripts" / "run_service.py"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# The web tier serves Jinja templates and vendored JavaScript from inside
# the package. They are data, not code, so PyInstaller does not find them
# by following imports - they have to be named.
APP_DATA = [
    (SRC / "jetpace_core" / "web" / "templates", "jetpace_core/web/templates"),
    (SRC / "jetpace_core" / "web" / "static", "jetpace_core/web/static"),
]

# Imports PyInstaller cannot see: chosen at runtime by name, or pulled in
# by a framework rather than by an import statement.
APP_HIDDEN = [
    "psycopg", "psycopg_binary",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "paho.mqtt.client",
    "pymodbus.client",
]

SUPERVISOR_HIDDEN = [
    "win32timezone",        # pywin32 loads this lazily inside the service
    "servicemanager", "win32serviceutil", "win32service", "win32event",
]


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def pyinstaller(name: str, entry: Path, hidden: list[str],
                data: list[tuple[Path, str]] | None = None,
                console: bool = True) -> Path:
    out = DIST / name
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name", name,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
        "--paths", str(SRC),
        "--console" if console else "--windowed",
    ]
    for h in hidden:
        args += ["--hidden-import", h]
    for source, dest in (data or []):
        if not source.exists():
            die(f"{source} is missing; the build would ship without it")
        args += ["--add-data", f"{source}{os.pathsep}{dest}"]
    args.append(str(entry))

    say(f"freezing {name} from {entry.relative_to(ROOT)}")
    started = time.monotonic()
    r = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                       timeout=1800)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-25:])
        die(f"PyInstaller failed for {name}:\n{tail}")
    if not out.is_dir():
        die(f"PyInstaller reported success but {out} does not exist")
    say(f"  {name} built in {time.monotonic() - started:.0f}s, "
        f"{size_of(out) / 1e6:.0f} MB")
    return out


def size_of(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def stage_payload(version: str) -> None:
    """
    Everything the install needs that is not frozen code.

    version.json sits at the root beside app/, because it is one of the
    two things a release replaces and the updater compares it against a
    manifest.
    """
    (DIST / "version.json").write_text(
        json.dumps({"version": version}, indent=2) + "\n", encoding="utf-8")
    say(f"wrote version.json ({version})")

    for name in ("config", "migrations"):
        source = ROOT / name
        if not source.is_dir():
            die(f"{source} is missing")
        target = DIST / name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target)
        say(f"copied {name}/ ({len(list(target.rglob('*')))} files)")


def stage_release(version: str) -> Path:
    """
    Assemble what a RELEASE contains, which is not what an install
    contains.

    An install has app/, supervisor/, config/, migrations/ and the data
    directory. A release carries only what it is allowed to replace -
    app/ and version.json - and make_release.py refuses a payload with
    anything else in it, which is the check that catches a build that
    would try to overwrite the supervisor or a customer's config.

    So the payload is staged on its own rather than by pointing the
    release tool at dist/.
    """
    payload = ROOT / "build" / "release"
    shutil.rmtree(payload, ignore_errors=True)
    payload.mkdir(parents=True)

    shutil.copytree(DIST / "app", payload / "app")
    shutil.copy2(DIST / "version.json", payload / "version.json")
    say(f"staged a release payload in {payload.relative_to(ROOT)} "
        f"({size_of(payload) / 1e6:.0f} MB)")
    return payload


def smoke_test() -> None:
    """
    Run the frozen executable and make it say where it thinks it is.

    This is the test that matters: `jetpace where` exercises the frozen
    path resolution, which is the thing most likely to be wrong - in a
    bundle __file__ points inside _internal, so an install root derived
    from it would be wrong in a way no source-checkout test can catch.
    """
    exe = DIST / "app" / "app.exe"
    if not exe.exists():
        exe = DIST / "app" / "jetpace.exe"
    if not exe.exists():
        die(f"no executable in {DIST / 'app'}")

    r = subprocess.run([str(exe), "where"], capture_output=True, text=True,
                       timeout=180)
    if r.returncode != 0:
        die(f"the frozen application would not run:\n{r.stdout}\n{r.stderr}")
    print()
    for line in r.stdout.strip().splitlines():
        print(f"   {line}")
    print()

    root = DIST.resolve()
    if str(root) not in r.stdout:
        say(f"WARNING: the frozen build resolved an install root that is not "
            f"{root}. Check jetpace_core.paths.install_root()")
    else:
        say("the frozen build resolves its own install root correctly")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="x.y.z")
    ap.add_argument("--app-only", action="store_true",
                    help="skip the supervisor (it is the slower half)")
    ap.add_argument("--clean", action="store_true",
                    help="delete dist/ and the PyInstaller work tree first")
    args = ap.parse_args()

    if not SEMVER.match(args.version):
        die(f"--version {args.version!r} is not x.y.z")
    if os.name != "nt":
        say("WARNING: a build made off Windows cannot be installed on it")

    if args.clean:
        shutil.rmtree(DIST, ignore_errors=True)
        shutil.rmtree(WORK, ignore_errors=True)
        say("cleaned dist/ and the work tree")

    DIST.mkdir(parents=True, exist_ok=True)
    print(f"\nBuilding {args.version}\n")

    app = pyinstaller("app", APP_ENTRY, APP_HIDDEN, APP_DATA)
    # PyInstaller names the executable after --name; the application is
    # reached as app\jetpace.exe, so rename it rather than naming the
    # whole bundle 'jetpace' and getting dist\jetpace\jetpace.exe.
    built = app / "app.exe"
    wanted = app / "jetpace.exe"
    if built.exists():
        built.replace(wanted)
        say("renamed app.exe to jetpace.exe")

    if not args.app_only:
        pyinstaller("supervisor", SUPERVISOR_ENTRY, SUPERVISOR_HIDDEN)

    stage_payload(args.version)
    smoke_test()
    payload = stage_release(args.version)

    print(f"\nBuilt into {DIST}  ({size_of(DIST) / 1e6:.0f} MB)\n")
    print("  what is NOT here yet: the vendored PostgreSQL and Mosquitto")
    print("  payloads, and an installer to lay it all down. See")
    print("  architecture.md for what remains.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
