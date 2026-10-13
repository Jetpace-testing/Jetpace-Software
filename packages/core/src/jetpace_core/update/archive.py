"""
Reading a release archive without trusting it.

A zip is a list of names and some bytes, and the names are whatever
whoever built it chose to put there. Handing them to the filesystem is how
an archive writes outside the folder it was supposed to unpack into - the
old "zip slip". The signature check in manifest.py makes that unlikely
here, but unlikely is not the standard for the one code path whose whole
job is to install code.

So nothing is extracted until every name has been looked at, and a name
that is not obviously fine is **refused** rather than cleaned up. Python's
own `ZipFile.extract` sanitises silently - it strips leading slashes and
drops `..` segments and carries on - which is worse for us than refusing:
a release containing such a name was either built wrong or built by someone
else, and in both cases the right answer is to stop and say so.

`zipfile.ZipFile.extractall` must never be used on a release.

Ported from Meril RTD's src/update/zip.ts. RTD had to write its own zip
reader because Node has none and a dependency for it would have had to
ship inside the very mechanism that installs dependencies. Python has
`zipfile` in the standard library, so only the checks are carried across -
and a few are added that RTD did not need.
"""

from __future__ import annotations

import posixpath
import re
import stat
import zipfile
from pathlib import Path

# A release of the application unpacks to tens of megabytes. These are
# "something is wrong" limits, not capacity planning: a 300-byte zip that
# expands to four gigabytes is not a release, it is a decompression bomb,
# and the manifest's signed byte count cannot catch it because the
# compressed size is honest.
MAX_UNCOMPRESSED = 512 * 1024 * 1024        # 512 MB in total
MAX_ENTRIES = 20_000
MAX_RATIO = 200                             # per entry, compressed:uncompressed

# Only what we write ourselves. In particular not ZIP64 oddities, not
# encryption, and not any of the exotic methods a general-purpose archiver
# might pick.
ALLOWED_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}

_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f]")


class ArchiveError(Exception):
    """A release archive that must not be unpacked, with the reason."""


def top_of(name: str) -> str:
    """`app/jetpace/main.py` -> `app`. A bare `version.json` -> itself."""
    head = posixpath.dirname(name)
    return name if head in ("", ".") else name.split("/")[0]


def _check_name(name: str) -> str:
    """
    Return the normalised name, or raise. Every rejection here is a thing
    an archive should not contain, so none of them is recoverable.
    """
    # A zip written on Windows by some tools uses backslashes even though
    # the format says forward. Normalise first, then judge - otherwise
    # `app\..\..\evil` passes a check that only looks for `../`.
    name = name.replace("\\", "/")

    if not name:
        raise ArchiveError("the archive has an entry with no name")
    if _CONTROL.search(name):
        raise ArchiveError(f"entry name contains a control character: {name!r}")
    if name.startswith("/"):
        raise ArchiveError(f"entry is an absolute path: {name}")
    if _DRIVE.match(name):
        raise ArchiveError(f"entry carries a drive letter: {name}")
    parts = name.split("/")
    if ".." in parts:
        raise ArchiveError(f"entry would climb out of the folder: {name}")
    if any(p in ("", ".") for p in parts[:-1]):
        raise ArchiveError(f"entry has an empty path segment: {name}")
    return name


def _check_member(info: zipfile.ZipInfo, name: str) -> None:
    if info.compress_type not in ALLOWED_METHODS:
        raise ArchiveError(
            f"entry {name} uses compression method {info.compress_type}, "
            f"which this reader does not accept")
    if info.flag_bits & 0x1:
        raise ArchiveError(f"entry {name} is encrypted")

    # A zip can carry a symlink in the high bits of external_attr. On
    # Windows it would usually fail to be created anyway, but a link
    # pointing at C:\Windows\System32 is not something to find out about
    # by trying.
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise ArchiveError(f"entry {name} is a symbolic link")

    if info.file_size > MAX_UNCOMPRESSED:
        raise ArchiveError(
            f"entry {name} expands to {info.file_size:,} bytes, over the "
            f"{MAX_UNCOMPRESSED:,} limit")
    if (info.compress_size > 0
            and info.file_size / info.compress_size > MAX_RATIO):
        raise ArchiveError(
            f"entry {name} expands {info.file_size // info.compress_size}x, "
            f"over the {MAX_RATIO}x limit; this looks like a "
            f"decompression bomb")


def inspect(zip_path: str | Path,
            allow_top: tuple[str, ...]) -> list[tuple[zipfile.ZipInfo, str]]:
    """
    Check an archive and return its file entries as (info, normalised name).
    Reads no file contents and writes nothing, so a refused archive has
    touched nothing.

    `allow_top` is the set of top-level names a release may contain -
    REPLACEABLE from layout.py. Anything else is refused here rather than
    at swap time, because an archive that cannot be installed should not be
    unpacked first.
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"not a readable zip file: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ArchiveError(
                f"the archive has {len(infos):,} entries, over the "
                f"{MAX_ENTRIES:,} limit")

        entries: list[tuple[zipfile.ZipInfo, str]] = []
        total = 0
        seen: set[str] = set()
        for info in infos:
            name = _check_name(info.filename)
            if info.is_dir():
                # Folders are created as the files inside them need them,
                # so a directory entry carries no information. Its name is
                # still checked above, because a hostile one is a signal.
                continue
            _check_member(info, name)

            top = top_of(name)
            if top not in allow_top:
                raise ArchiveError(
                    f"the release contains {top!r}, which an update may not "
                    f"replace. Allowed: {', '.join(allow_top)}")

            lowered = name.lower()
            if lowered in seen:
                # Two entries differing only in case collide on Windows, so
                # which one lands is a matter of ordering. A release must
                # not depend on that.
                raise ArchiveError(f"the archive has {name} twice")
            seen.add(lowered)

            total += info.file_size
            if total > MAX_UNCOMPRESSED:
                raise ArchiveError(
                    f"the archive expands to more than "
                    f"{MAX_UNCOMPRESSED:,} bytes")
            entries.append((info, name))

        if not entries:
            raise ArchiveError("the archive contains no files")
        return entries


def extract(zip_path: str | Path, dest: str | Path,
            allow_top: tuple[str, ...]) -> list[str]:
    """
    Unpack a checked archive into `dest`, which is created if missing.
    Returns the names written.

    Every name has already been checked by inspect(); the containment check
    is done again here against the resolved path, because the first check
    reasons about strings and this one reasons about where the file would
    actually land. Belt and braces on the one path that installs code.
    """
    dest = Path(dest).resolve()
    entries = inspect(zip_path, allow_top)
    dest.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info, name in entries:
            target = (dest / name).resolve()
            if target != dest and dest not in target.parents:
                raise ArchiveError(
                    f"entry would land outside {dest}: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            # zf.read() verifies the entry's CRC-32 and raises BadZipFile
            # if it does not match, so a corrupted download cannot be
            # written out as if it were fine.
            try:
                data = zf.read(info)
            except (zipfile.BadZipFile, OSError) as exc:
                raise ArchiveError(
                    f"entry {name} failed its checksum: {exc}") from exc
            target.write_bytes(data)
            written.append(name)
    return written


def version_in(zip_path: str | Path, allow_top: tuple[str, ...]) -> str | None:
    """
    The version the archive says it is, read from version.json inside it.

    Checked against the manifest before staging: if they disagree the build
    was mis-tagged, and installing it would leave version.json claiming
    something the files are not - after which every later update comparison
    is wrong.
    """
    import json

    entries = inspect(zip_path, allow_top)
    with zipfile.ZipFile(zip_path) as zf:
        for info, name in entries:
            if name == "version.json":
                try:
                    v = json.loads(zf.read(info).decode("utf-8"))["version"]
                except (ValueError, KeyError, UnicodeDecodeError) as exc:
                    raise ArchiveError(
                        f"version.json in the release is unreadable: "
                        f"{exc}") from exc
                if not isinstance(v, str) or not v:
                    raise ArchiveError(
                        "version.json in the release has no version string")
                return v
    return None
