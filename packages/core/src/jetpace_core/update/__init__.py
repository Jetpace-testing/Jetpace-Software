"""Signed, staged, self-rolling-back updates. Ported from Meril RTD."""

from .archive import ArchiveError
from .archive import extract as extract_archive
from .archive import inspect as inspect_archive
from .archive import top_of, version_in
from .installer import InstallError, Progress, check, install, rollback
from .layout import REPLACEABLE, Layout, layout_of
from .manifest import (
    Manifest,
    ManifestError,
    canonical,
    compare_versions,
    is_newer,
    sign_manifest,
    verify_manifest,
)
from .source import DirSource, GitHubSource, SourceError, UpdateSource
from .swap import (
    MAX_ATTEMPTS,
    MAX_SWAP_TRIES,
    BootResult,
    Pending,
    backup_version,
    clear_pending,
    confirm,
    prepare,
    read_pending,
    write_pending,
)

__all__ = [
    "ArchiveError", "extract_archive", "inspect_archive", "top_of",
    "version_in",
    "InstallError", "Progress", "check", "install", "rollback",
    "DirSource", "GitHubSource", "SourceError", "UpdateSource",
    "REPLACEABLE", "Layout", "layout_of",
    "Manifest", "ManifestError", "canonical", "compare_versions", "is_newer",
    "sign_manifest", "verify_manifest",
    "MAX_ATTEMPTS", "MAX_SWAP_TRIES", "BootResult", "Pending",
    "backup_version", "clear_pending", "confirm", "prepare", "read_pending",
    "write_pending",
]
