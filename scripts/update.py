"""
Check for, install and roll back releases from the command line.

    uv run python scripts/update.py --status
    uv run python scripts/update.py --check  --from-dir build
    uv run python scripts/update.py --install --from-dir build
    uv run python scripts/update.py --check  --repo Jetpace-testing/Jetpace-Software
    uv run python scripts/update.py --rollback

This is the update button before there is a button. It exercises exactly
the path the web interface will call, so whatever works here works there.

Installing does not apply anything: it downloads, verifies and stages, then
tells you to restart. The supervisor applies it on the way up, because a
running application cannot replace its own files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

from jetpace_core.update import (                                   # noqa: E402
    DirSource,
    GitHubSource,
    InstallError,
    Progress,
    backup_version,
    check,
    install,
    layout_of,
    read_pending,
    rollback,
)

DEFAULT_REPO = "Jetpace-testing/Jetpace-Software"


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def current_version(root: Path) -> str:
    try:
        v = json.loads((root / "version.json").read_text(encoding="utf-8"))
        return v["version"] if isinstance(v.get("version"), str) else "0.0.0"
    except (OSError, ValueError, KeyError, TypeError):
        return "0.0.0"


def source_for(args) -> DirSource | GitHubSource:
    if args.from_dir:
        return DirSource(args.from_dir)
    # The token is read from the environment here. On a site it comes from
    # the settings table, sealed, and is never handed back to the browser.
    token = os.environ.get("JETPACE_UPDATE_TOKEN", "")
    if not token:
        die("no release source.\n\n"
            "  Either point at a folder of releases:\n"
            "    --from-dir build\n\n"
            "  or set a GitHub token that can read the repository:\n"
            "    $env:JETPACE_UPDATE_TOKEN = '...'")
    return GitHubSource(args.repo or DEFAULT_REPO, token)


def show_progress(p: Progress) -> None:
    if p.of:
        pct = 100 * p.bytes / p.of
        print(f"\r  {p.phase:12} {p.bytes:>12,} / {p.of:,} bytes  "
              f"{pct:5.1f}%", end="", flush=True)
    else:
        print(f"\r  {p.phase:12}", end="", flush=True)
    if p.phase == "staged":
        print()


def do_status(root: Path) -> None:
    layout = layout_of(root)
    pending = read_pending(root)
    print()
    say(f"root      {root}")
    say(f"version   {current_version(root)}")
    say(f"backup    {backup_version(root) or 'none'}")
    if pending is None:
        say("pending   nothing")
    elif pending.staged:
        say(f"pending   {pending.to_version} is STAGED - restart to apply it "
            f"(asked by {pending.by})")
        if pending.swap_tries:
            say(f"          {pending.swap_tries} swap attempt(s) refused so "
                f"far; something is holding the install folder open")
    else:
        say(f"pending   {pending.to_version} is ON TRIAL, attempt "
            f"{pending.attempts} of 3")
    if layout.failed.exists():
        for d in sorted(layout.failed.iterdir()):
            say(f"failed    {d.name} is kept at {d}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--from-dir", type=Path,
                    help="a folder of manifests and zips (USB-stick case)")
    ap.add_argument("--repo", help=f"GitHub repo; default {DEFAULT_REPO}")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--by", default=os.environ.get("USERNAME", "cli"))
    args = ap.parse_args()

    root: Path = args.root
    if args.status or not (args.check or args.install or args.rollback):
        return do_status(root)

    if args.rollback:
        try:
            p = rollback(root, current_version(root), by=args.by)
        except InstallError as exc:
            die(str(exc))
        say(f"staged a return to {p.to_version}")
        say("restart the supervisor to apply it")
        return

    source = source_for(args)
    now = current_version(root)
    say(f"running {now}; asking {source.name}")
    try:
        manifest = check(source, now)
    except InstallError as exc:
        die(str(exc))

    if manifest is None:
        say("nothing newer is published")
        return

    say(f"{manifest.version} is available, {manifest.bytes:,} bytes")
    if manifest.notes:
        for line in manifest.notes.splitlines():
            say(f"  {line}")

    if not args.install:
        say("run again with --install to download and stage it")
        return

    try:
        pending = install(source, manifest, root, now, by=args.by,
                          on_progress=show_progress)
    except InstallError as exc:
        die(f"{exc}\n  Nothing was changed.")

    print()
    say(f"{pending.to_version} is staged and verified")
    say("restart the supervisor to apply it; it will be rolled back "
        "automatically if it fails its health gate")
    print()


if __name__ == "__main__":
    main()
