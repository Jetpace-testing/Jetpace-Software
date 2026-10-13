"""
Where an update lives while it is being applied.

Everything sits under `update\\` in the install folder, beside the
application itself, so a person looking at the PC can see what is going on
and a backup is one folder away from the files it backs up.

    update/pending.json      the update in flight - see swap.py
    update/staging/          the new release, unpacked, waiting for a swap
    update/backup/<ver>/     what the last update replaced
    update/failed/<ver>/     what a rolled-back update left behind
    update/downloads/        the zip, while it is being checked

Ported from Meril RTD's src/update/layout.ts, which has been applying
updates on Windows sites since 1.0.x. The shape is deliberately unchanged;
only the names of the replaceable things differ, because a frozen Python
application is one directory rather than dist/ plus node_modules/.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# What a release is allowed to replace. Anything else in a release zip is
# refused before it is unpacked - a release that tries to write outside
# this set is either built wrong or not ours.
#
# The supervisor is NOT in here, and cannot be: a running executable holds
# its own file open, so it can no more replace itself than the application
# could. Supervisor changes go out through the installer, which is the
# reason the supervisor must stay small and stay still.
REPLACEABLE: tuple[str, ...] = ("app", "version.json")


@dataclass(frozen=True)
class Layout:
    root: Path
    dir: Path
    pending: Path
    staging: Path
    backup: Path
    failed: Path
    downloads: Path

    def backup_for(self, version: str) -> Path:
        return self.backup / version

    def failed_for(self, version: str) -> Path:
        return self.failed / version


def layout_of(root: str | Path) -> Layout:
    root = Path(root).resolve()
    d = root / "update"
    return Layout(
        root=root,
        dir=d,
        pending=d / "pending.json",
        staging=d / "staging",
        backup=d / "backup",
        failed=d / "failed",
        downloads=d / "downloads",
    )
