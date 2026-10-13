"""
The install path: from "there is a newer release" to "it is staged, please
restart".

The order of the steps is the design, and it is the same order as Meril
RTD's updater.ts. Everything that can fail happens before anything in the
install folder is touched:

    1. verify the manifest's signature       nothing downloaded yet
    2. download the zip                      into update/downloads/
    3. check its length and SHA-256          against the signed manifest
    4. check every name in the archive       before anything is unpacked
    5. check the version inside the zip      against the manifest
    6. unpack into update/staging/
    7. write pending.json                    the point of no return
    8. ask the caller to restart             swap.prepare() does the rest

A failure at any step up to 7 leaves the install exactly as it was. Nothing
here writes to app/ - the running application cannot safely replace its own
files, so it does not try.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import archive
from .layout import REPLACEABLE, layout_of
from .manifest import Manifest, ManifestError, is_newer, verify_manifest
from .source import SourceError, UpdateSource
from .swap import Pending, read_pending, write_pending


class InstallError(Exception):
    """An install that did not happen, with a reason fit to show an admin."""


@dataclass
class Progress:
    phase: str          # checking | downloading | verifying | unpacking | staged
    bytes: int = 0
    of: int = 0


def check(source: UpdateSource, current_version: str) -> Manifest | None:
    """
    Ask the source for its newest release. Returns the manifest when it is
    newer than what is running, None when it is not.

    A manifest that fails verification raises rather than returning None.
    The difference matters: "nothing new" is routine and "someone published
    a release this site will not accept" is an event worth telling somebody
    about, because it is either a release signed with the wrong key or a
    release from someone else.
    """
    try:
        got = source.latest()
    except SourceError as exc:
        raise InstallError(str(exc)) from exc
    if got is None:
        return None

    raw, where = got
    try:
        manifest = verify_manifest(raw)
    except ManifestError as exc:
        raise InstallError(f"{where}: {exc}") from exc

    return manifest if is_newer(manifest.version, current_version) else None


def install(
    source: UpdateSource,
    manifest: Manifest,
    root: str | Path,
    current_version: str,
    by: str,
    on_progress: Callable[[Progress], None] | None = None,
) -> Pending:
    """
    Download, verify, unpack and stage. Returns the Pending that was
    written; the caller restarts the supervisor, and swap.prepare() applies
    it on the way up.
    """
    root = Path(root)
    layout = layout_of(root)

    if manifest is None:
        # check() returns None for "nothing newer", and a caller that
        # forwards that without looking should get a sentence rather than
        # an AttributeError from four frames down.
        raise InstallError("there is no update to install")
    if read_pending(root) is not None:
        raise InstallError(
            "an update is already staged and waiting for a restart")
    if not is_newer(manifest.version, current_version):
        raise InstallError(
            f"{manifest.version} is not newer than {current_version}")

    def say(phase: str, done: int = 0, of: int = 0) -> None:
        if on_progress:
            on_progress(Progress(phase, done, of))

    try:
        # 2. Download, into its own folder, emptied first so a half
        #    download from last time cannot be mistaken for this one.
        shutil.rmtree(layout.downloads, ignore_errors=True)
        layout.downloads.mkdir(parents=True, exist_ok=True)
        zip_path = layout.downloads / manifest.zip
        say("downloading", 0, manifest.bytes)
        source.download(manifest.zip, zip_path, manifest.bytes,
                        lambda n: say("downloading", n, manifest.bytes))

        # 3. The signed hash. Until this passes, the bytes on disk are just
        #    bytes somebody sent us.
        say("verifying", manifest.bytes, manifest.bytes)
        actual = _sha256(zip_path)
        if actual != manifest.sha256:
            raise InstallError(
                "the download does not match the signed hash; it was not "
                "installed")

        # 4 and 5. Names, then the version inside. Both before unpacking.
        say("unpacking", manifest.bytes, manifest.bytes)
        try:
            inside = archive.version_in(zip_path, REPLACEABLE)
        except archive.ArchiveError as exc:
            raise InstallError(str(exc)) from exc
        if inside is None:
            raise InstallError("the release has no version.json")
        if inside != manifest.version:
            raise InstallError(
                f"the release says {manifest.version} but the version.json "
                f"inside it says {inside}")

        # 6. Unpack. archive.extract checks every name again.
        shutil.rmtree(layout.staging, ignore_errors=True)
        try:
            archive.extract(zip_path, layout.staging, REPLACEABLE)
        except archive.ArchiveError as exc:
            raise InstallError(str(exc)) from exc
        if not (layout.staging / "app").exists():
            raise InstallError("the release has no app folder")

        shutil.rmtree(layout.downloads, ignore_errors=True)

        # 7. The point of no return: from here the next restart will swap.
        pending = Pending(
            from_version=current_version,
            to_version=manifest.version,
            staged=True,
            attempts=0,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            by=by,
        )
        write_pending(root, pending)
        say("staged", manifest.bytes, manifest.bytes)
        return pending

    except (InstallError, SourceError) as exc:
        # Leave nothing behind. staging in particular: a leftover folder
        # plus a pending.json written by some later attempt would swap in a
        # release nobody verified.
        shutil.rmtree(layout.downloads, ignore_errors=True)
        shutil.rmtree(layout.staging, ignore_errors=True)
        raise InstallError(str(exc)) from exc
    except OSError as exc:
        shutil.rmtree(layout.downloads, ignore_errors=True)
        shutil.rmtree(layout.staging, ignore_errors=True)
        raise InstallError(f"the install failed: {exc}") from exc


def rollback(root: str | Path, current_version: str, by: str) -> Pending:
    """
    Put the backed-up version back, the same way an update goes on: it is
    moved into staging, pending.json points at it, and the restart does the
    swap. What is running now becomes the backup.

    Deliberately the same path as an install rather than a special case.
    One mechanism that is exercised on every update is worth more than a
    second one used only when something has already gone wrong.
    """
    from .swap import backup_version

    root = Path(root)
    layout = layout_of(root)
    if read_pending(root) is not None:
        raise InstallError(
            "an update is already staged and waiting for a restart")

    target = backup_version(root)
    if target is None:
        raise InstallError("there is no previous version to go back to")

    backup = layout.backup_for(target)
    shutil.rmtree(layout.staging, ignore_errors=True)
    layout.dir.mkdir(parents=True, exist_ok=True)
    try:
        backup.rename(layout.staging)
    except OSError as exc:
        raise InstallError(f"could not stage {target}: {exc}") from exc

    pending = Pending(
        from_version=current_version,
        to_version=target,
        staged=True,
        attempts=0,
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        by=by,
    )
    write_pending(root, pending)
    return pending


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
