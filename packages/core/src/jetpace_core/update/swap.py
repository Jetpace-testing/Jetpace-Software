"""
Swapping a new release in, and putting the old one back when it will not
start.

This is the part of an update that cannot be done by the thing being
updated. A running executable holds its own files open, and Windows will
not rename an open file - so the application can stage a release, but it
can never apply one to itself. The supervisor applies it, before it starts
any child, when nothing of the application is loaded.

`pending.json` is the record of an update in flight, and it is the only
thing that survives a power cut mid-update:

    staged: true     the new release is in update/staging/, awaiting a swap
    staged: false    it has been swapped in and is being given a chance to
                     start; `attempts` counts the tries

The supervisor deletes the file once the new release is up and has passed
its health gate. If instead it keeps dying, the supervisor keeps restarting
it, this keeps counting, and after MAX_ATTEMPTS the backup goes back. A bad
release costs a few minutes, not a site visit.

Ported from Meril RTD's src/update/bootstrap.ts, including the two-pass
swap and the rename retry, both of which exist because of specific
failures seen on real sites.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .layout import REPLACEABLE, Layout, layout_of

MAX_ATTEMPTS = 3
MAX_SWAP_TRIES = 3

# How long to keep trying a rename Windows refuses, and how often. Five
# seconds in total: long enough for an antivirus scan of freshly written
# files to finish, short enough not to delay a start that is never going to
# succeed.
_RENAME_TRIES = 20
_RENAME_WAIT = 0.25

# Windows refuses a rename with a sharing violation or access denied while
# anything holds a file in the folder; POSIX uses EACCES/EPERM/ENOTEMPTY.
# Anything outside this set is a real error and is raised immediately.
_RETRYABLE_ERRNO = {errno.EACCES, errno.EPERM, errno.EBUSY, errno.ENOTEMPTY}
_RETRYABLE_WINERROR = {5, 32, 145, 183}


class SwapBroken(OSError):
    """
    The swap failed and the undo failed too, so the install folder holds
    neither release whole.

    Distinct from an ordinary refused swap because the handling differs:
    a refused swap is retried at the next restart and nothing is lost, but
    this needs a person, and telling them which folder the files are in is
    the difference between a ten-minute fix and a reinstall.
    """


@dataclass(frozen=True)
class Pending:
    from_version: str
    to_version: str
    staged: bool
    attempts: int
    at: str                 # ISO time the update was staged
    by: str                 # who asked for it, for the log
    swap_tries: int = 0     # swaps refused because a folder was held open

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"


@dataclass
class BootResult:
    """What prepare() did, so the caller can log it once it has a logger."""
    applied: Pending | None = None      # the new release was swapped in
    rolled_back: Pending | None = None  # it would not start; backup restored
    waiting: Pending | None = None      # swap refused, kept for next restart
    attempt: int | None = None          # which try of the new release this is
    error: str | None = None            # the swap itself went wrong

    def __bool__(self) -> bool:
        return any((self.applied, self.rolled_back, self.waiting,
                    self.attempt, self.error))

    def describe(self) -> str:
        if self.error:
            return self.error
        if self.rolled_back:
            return (f"{self.rolled_back.to_version} would not start and "
                    f"{self.rolled_back.from_version} was put back")
        if self.applied:
            return f"updated to {self.applied.to_version}"
        if self.waiting:
            return (f"update to {self.waiting.to_version} is waiting for a "
                    f"restart")
        if self.attempt:
            return f"starting the new release, try {self.attempt}"
        return "nothing to do"


# --- pending.json ----------------------------------------------------------

def read_pending(root: str | Path) -> Pending | None:
    path = layout_of(root).pending
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return Pending(
            from_version=str(d["from_version"]),
            to_version=str(d["to_version"]),
            staged=bool(d["staged"]),
            attempts=int(d.get("attempts", 0)),
            at=str(d.get("at", "")),
            by=str(d.get("by", "")),
            swap_tries=int(d.get("swap_tries", 0)),
        )
    except (ValueError, KeyError, TypeError, OSError):
        # A truncated pending.json - a power cut while writing it - must not
        # stop the service starting. Treated as no update in flight, which
        # leaves the install exactly as it is.
        return None


def write_pending(root: str | Path, p: Pending) -> None:
    layout = layout_of(root)
    layout.dir.mkdir(parents=True, exist_ok=True)
    tmp = layout.pending.with_suffix(".json.tmp")
    tmp.write_text(p.to_json(), encoding="utf-8")
    os.replace(tmp, layout.pending)      # atomic; never a half-written file


def clear_pending(root: str | Path) -> None:
    layout_of(root).pending.unlink(missing_ok=True)


# --- moving directories about ---------------------------------------------

def _rename_waiting(src: Path, dst: Path) -> None:
    """
    Rename, retrying while Windows says the folder is held open.

    An Explorer window sitting in app/, an antivirus scanning files copied a
    minute ago, a log tail - any of them refuses the rename. The scan passes
    in seconds, so a refusal is retried for five before it counts as one.
    """
    for attempt in range(1, _RENAME_TRIES + 1):
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            retryable = (exc.errno in _RETRYABLE_ERRNO
                         or getattr(exc, "winerror", None) in _RETRYABLE_WINERROR)
            if attempt >= _RENAME_TRIES or not retryable:
                raise
            time.sleep(_RENAME_WAIT)


def _swap_in(root: Path, source: Path, aside: Path) -> None:
    """
    Move every replaceable entry present in `source` into `root`, parking
    what root has into `aside` first.

    Two passes, and the order is the point: nothing moves out of `source`
    until everything in `root` that would be overwritten is safely aside. If
    a rename then fails half way, what was parked goes back and the install
    is as it was. A single-pass version leaves the install folder with some
    files from each release, which is unbootable and very hard to explain.
    """
    names = [n for n in REPLACEABLE if (source / n).exists()]
    if not names:
        raise OSError(f"nothing to install in {source}")

    aside.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    try:
        for n in names:
            current = root / n
            if current.exists():
                target = aside / n
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True) \
                        if target.is_dir() else target.unlink(missing_ok=True)
                _rename_waiting(current, target)
                moved.append(n)
        for n in names:
            _rename_waiting(source / n, root / n)
    except OSError as exc:
        # Put back whatever was parked. This uses the same retrying rename,
        # because whatever refused the move in will usually also refuse the
        # move back, and the undo is the more important of the two.
        stranded: list[str] = []
        for n in moved:
            back = root / n
            parked = aside / n
            if not back.exists() and parked.exists():
                try:
                    _rename_waiting(parked, back)
                except OSError:
                    stranded.append(n)
        if stranded:
            # The install is now incomplete, which is worse than either
            # release. It must be said loudly and precisely - this is the
            # one outcome that needs a person, and the files still exist.
            raise SwapBroken(
                f"the swap failed ({exc}) and the previous "
                f"{', '.join(stranded)} could not be put back. The install "
                f"is incomplete. The files are in {aside} and must be moved "
                f"back into {root} by hand.") from exc
        raise


# --- the state machine ----------------------------------------------------

def prepare(root: str | Path) -> BootResult:
    """
    Called by the supervisor before it starts anything. Returns what it did;
    it logs nothing itself and it never raises - a broken update must not
    stop the previous release from starting.
    """
    try:
        return _prepare(layout_of(root))
    except Exception as exc:                                # noqa: BLE001
        # Defensive: _prepare handles its own failures, but this function is
        # the first thing to run on every boot and must not be the reason a
        # site is down.
        return BootResult(error=f"update check failed: {exc}")


def _prepare(l: Layout) -> BootResult:
    p = read_pending(l.root)
    if p is None:
        return BootResult()

    if p.staged:
        return _apply_staged(l, p)
    return _judge_running(l, p)


def _apply_staged(l: Layout, p: Pending) -> BootResult:
    try:
        _swap_in(l.root, l.staging, l.backup_for(p.from_version))
    except SwapBroken as exc:
        # Not retryable. Trying again cannot help, and leaving pending.json
        # in place would hide this behind a second attempt's message.
        clear_pending(l.root)
        return BootResult(error=str(exc))
    except OSError as exc:
        # Refused, almost always a held folder. The release stays in staging
        # for the next restart, up to a point. The old files are back in
        # place, so the service starts on what already worked.
        tries = p.swap_tries + 1
        if tries < MAX_SWAP_TRIES:
            nxt = replace(p, swap_tries=tries)
            write_pending(l.root, nxt)
            return BootResult(
                waiting=nxt,
                error=(f"update to {p.to_version} could not be applied "
                       f"(try {tries} of {MAX_SWAP_TRIES}): {exc}. Is a "
                       f"window or program open in the install folder? It "
                       f"will be tried again at the next restart"))
        clear_pending(l.root)
        shutil.rmtree(l.staging, ignore_errors=True)
        return BootResult(
            error=(f"update to {p.to_version} could not be applied after "
                   f"{tries} tries and was given up: {exc}"))

    try:
        shutil.rmtree(l.staging, ignore_errors=True)
        # Only the newest backup is kept. It sits beside the data it
        # protects and a site PC's disk is not large.
        if l.backup.exists():
            for old in l.backup.iterdir():
                if old.name != p.from_version:
                    shutil.rmtree(old, ignore_errors=True)
        nxt = replace(p, staged=False, attempts=1, swap_tries=0)
        write_pending(l.root, nxt)
        return BootResult(applied=nxt, attempt=1)
    except OSError as exc:
        clear_pending(l.root)
        return BootResult(
            error=(f"update to {p.to_version} was applied but could not be "
                   f"tidied: {exc}"))


def _judge_running(l: Layout, p: Pending) -> BootResult:
    """The new release is in place and has not yet confirmed itself."""
    if p.attempts < MAX_ATTEMPTS:
        nxt = replace(p, attempts=p.attempts + 1)
        write_pending(l.root, nxt)
        return BootResult(attempt=nxt.attempts)

    # Three starts, three deaths. Back to what worked.
    backup = l.backup_for(p.from_version)
    if not backup.exists():
        clear_pending(l.root)
        return BootResult(
            error=(f"release {p.to_version} will not start and there is no "
                   f"backup of {p.from_version} to restore"))
    try:
        _swap_in(l.root, backup, l.failed_for(p.to_version))
        shutil.rmtree(backup, ignore_errors=True)
        clear_pending(l.root)
        return BootResult(rolled_back=p)
    except OSError as exc:
        clear_pending(l.root)
        return BootResult(
            error=f"could not roll back to {p.from_version}: {exc}")


def confirm(root: str | Path) -> Pending | None:
    """
    The new release is up and has passed its health gate; the update is
    over. Returns what was confirmed, for the log.
    """
    p = read_pending(root)
    if p is None or p.staged:
        return None
    clear_pending(root)
    return p


def backup_version(root: str | Path) -> str | None:
    """The newest version sitting in update/backup/, if any."""
    from .manifest import SEMVER, compare_versions
    import functools

    l = layout_of(root)
    if not l.backup.exists():
        return None
    versions = [d.name for d in l.backup.iterdir()
                if d.is_dir() and SEMVER.match(d.name)
                and (d / "app").exists()]
    if not versions:
        return None
    versions.sort(key=functools.cmp_to_key(compare_versions))
    return versions[-1]
