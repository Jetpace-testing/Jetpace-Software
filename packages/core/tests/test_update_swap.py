"""
The swap state machine.

This code runs on a site, unattended, at the moment something has already
gone wrong - a release that will not start, a folder held open by an
antivirus scan, a power cut halfway through. Nobody is watching and there
is no second chance, so every branch is tested here, including the ones
that only happen after a failure.

The install tree these tests build is the real shape:

    root/
      app/                 what a release replaces
      version.json
      update/
        pending.json
        staging/app/       the incoming release
        backup/<ver>/app/  what the last one replaced
"""

import json
import os
from pathlib import Path

import pytest

from jetpace_core.update import layout_of
from jetpace_core.update.swap import (
    MAX_ATTEMPTS,
    MAX_SWAP_TRIES,
    Pending,
    backup_version,
    clear_pending,
    confirm,
    prepare,
    read_pending,
    write_pending,
)


def make_install(root: Path, version: str = "1.0.0", marker: str = "old") -> None:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "main.py").write_text(f"# {marker}\n")
    (root / "version.json").write_text(json.dumps({"version": version}))


def stage_release(root: Path, version: str = "1.1.0", marker: str = "new") -> None:
    staging = layout_of(root).staging
    (staging / "app").mkdir(parents=True, exist_ok=True)
    (staging / "app" / "main.py").write_text(f"# {marker}\n")
    (staging / "version.json").write_text(json.dumps({"version": version}))


def marker_of(root: Path) -> str:
    return (root / "app" / "main.py").read_text().strip().lstrip("# ")


def pending_for(frm: str = "1.0.0", to: str = "1.1.0", **kw) -> Pending:
    base = dict(from_version=frm, to_version=to, staged=True, attempts=0,
                at="2026-10-12T09:00:00+00:00", by="test")
    base.update(kw)
    return Pending(**base)


# --- nothing to do --------------------------------------------------------

def test_no_pending_file_does_nothing(tmp_path):
    make_install(tmp_path)
    result = prepare(tmp_path)
    assert not result
    assert marker_of(tmp_path) == "old"


def test_a_truncated_pending_file_does_not_stop_the_start(tmp_path):
    """
    A power cut while writing pending.json leaves half a file. That must
    read as "no update in flight" and leave the install alone - never as a
    crash, because this runs before anything else on every boot.
    """
    make_install(tmp_path)
    l = layout_of(tmp_path)
    l.dir.mkdir(parents=True)
    l.pending.write_text('{"from_version": "1.0.0", "staged": tr')
    assert read_pending(tmp_path) is None
    assert not prepare(tmp_path)
    assert marker_of(tmp_path) == "old"


# --- applying a staged release --------------------------------------------

def test_a_staged_release_is_swapped_in(tmp_path):
    make_install(tmp_path)
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for())

    result = prepare(tmp_path)

    assert result.applied is not None
    assert result.attempt == 1
    assert marker_of(tmp_path) == "new"
    assert json.loads((tmp_path / "version.json").read_text())["version"] == "1.1.0"


def test_the_old_release_is_kept_as_a_backup(tmp_path):
    make_install(tmp_path)
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for())

    prepare(tmp_path)

    backup = layout_of(tmp_path).backup_for("1.0.0")
    assert (backup / "app" / "main.py").read_text().strip() == "# old"
    assert backup_version(tmp_path) == "1.0.0"


def test_staging_is_emptied_after_a_successful_swap(tmp_path):
    make_install(tmp_path)
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for())
    prepare(tmp_path)
    assert not layout_of(tmp_path).staging.exists()


def test_the_swap_moves_pending_on_to_the_trial_phase(tmp_path):
    """
    After the swap the update is not over: the new release still has to
    start. pending.json flips to staged=false with attempts=1, which is
    what makes a crash loop recoverable instead of permanent.
    """
    make_install(tmp_path)
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for())
    prepare(tmp_path)

    p = read_pending(tmp_path)
    assert p is not None and p.staged is False and p.attempts == 1


def test_only_the_newest_backup_is_kept(tmp_path):
    """A site PC's disk is not large, and old backups are never used."""
    make_install(tmp_path)
    l = layout_of(tmp_path)
    for stale in ("0.8.0", "0.9.0"):
        (l.backup_for(stale) / "app").mkdir(parents=True)
        (l.backup_for(stale) / "app" / "main.py").write_text("# stale\n")
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for())

    prepare(tmp_path)

    assert sorted(d.name for d in l.backup.iterdir()) == ["1.0.0"]


# --- giving the new release a chance to start ------------------------------

def test_each_failed_start_counts_up(tmp_path):
    make_install(tmp_path, "1.1.0", "new")
    write_pending(tmp_path, pending_for(staged=False, attempts=1))

    assert prepare(tmp_path).attempt == 2
    assert prepare(tmp_path).attempt == 3
    assert marker_of(tmp_path) == "new"      # still trying


def test_after_the_last_attempt_the_backup_goes_back(tmp_path):
    make_install(tmp_path, "1.1.0", "new")
    l = layout_of(tmp_path)
    (l.backup_for("1.0.0") / "app").mkdir(parents=True)
    (l.backup_for("1.0.0") / "app" / "main.py").write_text("# old\n")
    (l.backup_for("1.0.0") / "version.json").write_text('{"version": "1.0.0"}')
    write_pending(tmp_path, pending_for(staged=False, attempts=MAX_ATTEMPTS))

    result = prepare(tmp_path)

    assert result.rolled_back is not None
    assert result.rolled_back.to_version == "1.1.0"
    assert marker_of(tmp_path) == "old"
    assert read_pending(tmp_path) is None     # the update is over


def test_the_failed_release_is_kept_for_diagnosis(tmp_path):
    """
    A release that would not start is the one you most want to look at, so
    it is moved aside rather than deleted.
    """
    make_install(tmp_path, "1.1.0", "new")
    l = layout_of(tmp_path)
    (l.backup_for("1.0.0") / "app").mkdir(parents=True)
    (l.backup_for("1.0.0") / "app" / "main.py").write_text("# old\n")
    write_pending(tmp_path, pending_for(staged=False, attempts=MAX_ATTEMPTS))

    prepare(tmp_path)

    assert (l.failed_for("1.1.0") / "app" / "main.py").read_text().strip() == "# new"


def test_a_rollback_with_no_backup_says_so_and_keeps_running(tmp_path):
    """
    The worst case: the new release will not start and there is nothing to
    go back to. It must report that clearly and leave the install alone -
    a half-removed install is worse than a broken one.
    """
    make_install(tmp_path, "1.1.0", "new")
    write_pending(tmp_path, pending_for(staged=False, attempts=MAX_ATTEMPTS))

    result = prepare(tmp_path)

    assert result.error is not None
    assert "no backup" in result.error
    assert marker_of(tmp_path) == "new"
    assert read_pending(tmp_path) is None     # do not loop on it forever


# --- a refused swap -------------------------------------------------------

def test_a_refused_swap_keeps_the_release_for_the_next_restart(tmp_path, monkeypatch):
    """
    Windows refuses to rename a folder anything holds open - an Explorer
    window, an antivirus mid-scan. The old files stay in place, the release
    waits in staging, and the service starts on what already worked.
    """
    make_install(tmp_path)
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for())

    import jetpace_core.update.swap as swap
    monkeypatch.setattr(swap, "_RENAME_TRIES", 1)
    monkeypatch.setattr(swap, "_RENAME_WAIT", 0)
    monkeypatch.setattr(swap, "_rename_waiting",
                        lambda s, d: (_ for _ in ()).throw(
                            PermissionError(13, "held open")))

    result = prepare(tmp_path)

    assert result.waiting is not None
    assert "try 1 of" in result.error
    assert marker_of(tmp_path) == "old"                   # untouched
    assert layout_of(tmp_path).staging.exists()            # still there
    assert read_pending(tmp_path).swap_tries == 1


def test_a_swap_refused_too_often_is_given_up(tmp_path, monkeypatch):
    make_install(tmp_path)
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for(swap_tries=MAX_SWAP_TRIES - 1))

    import jetpace_core.update.swap as swap
    monkeypatch.setattr(swap, "_rename_waiting",
                        lambda s, d: (_ for _ in ()).throw(
                            PermissionError(13, "held open")))

    result = prepare(tmp_path)

    assert result.waiting is None
    assert "given up" in result.error
    assert read_pending(tmp_path) is None
    assert not layout_of(tmp_path).staging.exists()
    assert marker_of(tmp_path) == "old"


def test_a_failure_part_way_through_leaves_the_install_whole(tmp_path, monkeypatch):
    """
    The two-pass swap exists for this. If the incoming files fail to move
    after the old ones have been parked, what was parked must come back -
    otherwise the install folder holds some files from each release, which
    will not start and is very hard to explain.
    """
    make_install(tmp_path)
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for())

    import jetpace_core.update.swap as swap
    real = os.rename
    staging = str(layout_of(tmp_path).staging)

    def refuse_moves_out_of_staging(src, dst):
        # Only the incoming files are blocked. Parking the old ones aside,
        # and putting them back, both still work - which is the situation
        # the two-pass order is designed for.
        if str(src).startswith(staging):
            raise PermissionError(13, "held open")
        return real(src, dst)

    monkeypatch.setattr(swap.os, "rename", refuse_moves_out_of_staging)
    monkeypatch.setattr(swap, "_RENAME_TRIES", 1)
    monkeypatch.setattr(swap, "_RENAME_WAIT", 0)

    result = prepare(tmp_path)

    monkeypatch.undo()
    assert result.waiting is not None               # kept for another try
    assert (tmp_path / "app" / "main.py").exists()
    assert marker_of(tmp_path) == "old"
    assert json.loads((tmp_path / "version.json").read_text())["version"] == "1.0.0"


def test_an_undo_that_also_fails_is_reported_loudly(tmp_path, monkeypatch):
    """
    The worst outcome in this module: the swap fails, and putting the old
    files back fails too, so the install holds neither release whole. It
    must not be retried silently or swallowed - it needs a person, and the
    message has to say which folder the files are in.
    """
    make_install(tmp_path)
    stage_release(tmp_path)
    write_pending(tmp_path, pending_for())

    import jetpace_core.update.swap as swap
    real = os.rename
    root = str(tmp_path)

    def refuse_everything_after_parking(src, dst):
        # Parking works; both the move in and the move back are refused.
        if str(dst).startswith(str(layout_of(tmp_path).backup)):
            return real(src, dst)
        raise PermissionError(13, "held open")

    monkeypatch.setattr(swap.os, "rename", refuse_everything_after_parking)
    monkeypatch.setattr(swap, "_RENAME_TRIES", 1)
    monkeypatch.setattr(swap, "_RENAME_WAIT", 0)

    result = prepare(tmp_path)
    monkeypatch.undo()

    assert result.waiting is None                   # never retried
    assert result.error is not None
    assert "install is incomplete" in result.error
    assert "by hand" in result.error
    assert str(layout_of(tmp_path).backup_for("1.0.0")) in result.error
    assert read_pending(tmp_path) is None


# --- confirming -----------------------------------------------------------

def test_confirm_ends_the_update(tmp_path):
    make_install(tmp_path, "1.1.0", "new")
    write_pending(tmp_path, pending_for(staged=False, attempts=1))

    done = confirm(tmp_path)

    assert done is not None and done.to_version == "1.1.0"
    assert read_pending(tmp_path) is None


def test_confirm_refuses_while_a_release_is_still_only_staged(tmp_path):
    """
    staged=true means the swap has not happened. Confirming then would
    delete the record of an update that is still waiting, stranding the
    release in staging forever.
    """
    make_install(tmp_path)
    write_pending(tmp_path, pending_for(staged=True))
    assert confirm(tmp_path) is None
    assert read_pending(tmp_path) is not None


def test_confirm_with_nothing_pending_is_harmless(tmp_path):
    make_install(tmp_path)
    assert confirm(tmp_path) is None


# --- pending.json round trip ----------------------------------------------

def test_pending_survives_a_write_and_read(tmp_path):
    p = pending_for(frm="1.0.0", to="2.0.0", staged=False, attempts=2,
                    swap_tries=1)
    write_pending(tmp_path, p)
    assert read_pending(tmp_path) == p


def test_writing_pending_is_atomic(tmp_path):
    """
    Written to a temp file and renamed, so a power cut cannot leave a
    half-written pending.json that reads as a different update.
    """
    write_pending(tmp_path, pending_for())
    leftovers = list(layout_of(tmp_path).dir.glob("*.tmp"))
    assert leftovers == []


def test_clear_pending_is_idempotent(tmp_path):
    clear_pending(tmp_path)
    write_pending(tmp_path, pending_for())
    clear_pending(tmp_path)
    clear_pending(tmp_path)
    assert read_pending(tmp_path) is None


def test_backup_version_ignores_junk_in_the_backup_folder(tmp_path):
    l = layout_of(tmp_path)
    for name in ("1.0.0", "0.9.0", "notaversion", "1.0.0.0"):
        (l.backup_for(name) / "app").mkdir(parents=True)
    (l.backup / "a-stray-file.txt").parent.mkdir(parents=True, exist_ok=True)
    (l.backup / "a-stray-file.txt").write_text("x")
    assert backup_version(tmp_path) == "1.0.0"


def test_backup_version_needs_an_actual_app_folder(tmp_path):
    """An empty version folder is not something to roll back to."""
    l = layout_of(tmp_path)
    l.backup_for("1.0.0").mkdir(parents=True)
    assert backup_version(tmp_path) is None
