"""
Reading a release archive without trusting it.

Every test here builds a hostile zip by hand and proves it is refused.
That is the point: this is the code path that installs code, so the
interesting cases are not well-formed releases but the ones that are
trying to get somewhere they should not.

Archives are built with raw ZipInfo rather than by zipping a directory,
because the whole class of attack lives in names a filesystem would never
have produced.
"""

import zipfile

import pytest

from jetpace_core.update.archive import (
    MAX_ENTRIES,
    MAX_RATIO,
    ArchiveError,
    extract,
    inspect,
    top_of,
    version_in,
)

ALLOW = ("app", "version.json")


def build(tmp_path, entries, name="release.zip", method=zipfile.ZIP_DEFLATED):
    """entries: {name: bytes | (bytes, external_attr)}"""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", method) as z:
        for entry, value in entries.items():
            data, attr = value if isinstance(value, tuple) else (value, None)
            info = zipfile.ZipInfo(entry)
            info.compress_type = method
            if attr is not None:
                info.external_attr = attr
            z.writestr(info, data)
    return path


def good(tmp_path, **kw):
    return build(tmp_path, {
        "app/main.py": b"print('hello')\n",
        "app/sub/thing.py": b"x = 1\n",
        "version.json": b'{"version": "1.1.0"}',
    }, **kw)


# --- the well-formed case -------------------------------------------------

def test_a_good_release_is_accepted(tmp_path):
    entries = inspect(good(tmp_path), ALLOW)
    assert sorted(n for _, n in entries) == [
        "app/main.py", "app/sub/thing.py", "version.json"]


def test_extracting_writes_the_files_where_expected(tmp_path):
    dest = tmp_path / "staging"
    written = extract(good(tmp_path), dest, ALLOW)

    assert sorted(written) == ["app/main.py", "app/sub/thing.py",
                               "version.json"]
    assert (dest / "app" / "main.py").read_bytes() == b"print('hello')\n"
    assert (dest / "app" / "sub" / "thing.py").read_bytes() == b"x = 1\n"


def test_stored_entries_work_too(tmp_path):
    """Small files are stored rather than deflated; both must read."""
    dest = tmp_path / "staging"
    extract(good(tmp_path, method=zipfile.ZIP_STORED), dest, ALLOW)
    assert (dest / "version.json").exists()


def test_directory_entries_are_ignored_not_refused(tmp_path):
    path = tmp_path / "r.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("app/", b"")
        z.writestr("app/main.py", b"x")
        z.writestr("version.json", b'{"version":"1.0.0"}')
    assert sorted(n for _, n in inspect(path, ALLOW)) == [
        "app/main.py", "version.json"]


def test_the_version_inside_the_zip_can_be_read(tmp_path):
    assert version_in(good(tmp_path), ALLOW) == "1.1.0"


# --- escaping the folder --------------------------------------------------

@pytest.mark.parametrize("name", [
    "../evil.py",
    "../../evil.py",
    "app/../../evil.py",
    "app/../../../Windows/System32/evil.dll",
    "..",
])
def test_an_entry_that_climbs_out_is_refused(tmp_path, name):
    path = build(tmp_path, {name: b"x", "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="climb out|no name|empty path"):
        inspect(path, ALLOW)


def test_a_backslash_path_is_normalised_before_it_is_judged(tmp_path):
    """
    Some Windows tools write backslashes even though the format says
    forward slashes. Checking for '../' before normalising would let
    `app\\..\\..\\evil` through - so normalisation happens first.
    """
    path = build(tmp_path, {"app\\..\\..\\evil.py": b"x",
                            "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="climb out"):
        inspect(path, ALLOW)


@pytest.mark.parametrize("name", [
    "/etc/passwd",
    "/app/main.py",
    "C:/Windows/evil.dll",
    "c:evil.dll",
    "D:/Jetpace/app/main.py",
])
def test_an_absolute_path_is_refused(tmp_path, name):
    path = build(tmp_path, {name: b"x", "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="absolute path|drive letter"):
        inspect(path, ALLOW)


@pytest.mark.parametrize("name", [
    "app/main.py\x00.txt",
    "app/ma\x00in.py",
    "app/main.py\n",
    "app/\x1fmain.py",
])
def test_a_control_character_in_a_name_is_refused(name):
    """
    A NUL truncates a path in some C APIs, so a name checked as one thing
    can be written as another.

    The guard is exercised directly, because it cannot be reached through
    zipfile - see the test below for why. It is kept for names that arrive
    from anywhere else, and because a guard whose absence is invisible is
    the worst kind to remove.
    """
    from jetpace_core.update.archive import _check_name

    with pytest.raises(ArchiveError, match="control character"):
        _check_name(name)


def test_pythons_zipfile_truncates_a_name_at_a_null_before_we_see_it(tmp_path):
    """
    Documenting a property of the standard library that this module relies
    on, so nobody later deletes the control-character guard as dead code -
    or, worse, assumes it is what protects us here.

    A name stored as `app/mai<NUL>.py` is read back as `app/mai`. That is
    benign for the escape question, and the reason is worth stating:
    truncation only ever SHORTENS a path, and a shorter path cannot climb
    further out than a longer one. `app/x<NUL>/../../evil` truncates to
    `app/x`, which is inside the folder.
    """
    path = build(tmp_path, {"app/main.py": b"x", "version.json": b"{}"},
                 method=zipfile.ZIP_STORED)
    # Same length, so every offset in the archive stays valid. The name is
    # in both the local header and the central directory.
    path.write_bytes(path.read_bytes()
                     .replace(b"app/main.py", b"app/mai\x00.py"))

    names = [i.filename for i in zipfile.ZipFile(path).infolist()]
    assert "app/mai" in names
    assert not any("\x00" in n for n in names)

    # And so the archive is accepted - correctly, because what it now
    # names is a harmless file inside app/.
    assert sorted(n for _, n in inspect(path, ALLOW)) == ["app/mai",
                                                          "version.json"]


def test_a_newline_in_a_name_is_refused(tmp_path):
    path = build(tmp_path, {"app/ma\nin.py": b"x", "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="control character"):
        inspect(path, ALLOW)


def test_nothing_is_written_when_an_archive_is_refused(tmp_path):
    """
    The check happens before any file is created, so a refused archive
    leaves no partial unpack behind to be swapped in later.
    """
    path = build(tmp_path, {"app/good.py": b"x", "../evil.py": b"x",
                            "version.json": b"{}"})
    dest = tmp_path / "staging"
    with pytest.raises(ArchiveError):
        extract(path, dest, ALLOW)
    assert not dest.exists() or not list(dest.rglob("*"))


# --- the replaceable allowlist -------------------------------------------

@pytest.mark.parametrize("top", [
    "supervisor.exe",      # the one thing OTA must never replace
    "update",              # would clobber the staging folder mid-update
    ".local",              # secrets
    "logs",
    "migrations",
    "anything-else",
])
def test_a_top_level_name_outside_the_allowlist_is_refused(tmp_path, top):
    path = build(tmp_path, {f"{top}/x": b"x", "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="may not replace"):
        inspect(path, ALLOW)


def test_a_stray_file_at_the_root_is_refused(tmp_path):
    path = build(tmp_path, {"app/main.py": b"x", "README.md": b"x",
                            "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="may not replace"):
        inspect(path, ALLOW)


def test_the_allowlist_is_a_parameter_not_a_constant(tmp_path):
    """
    Passed in rather than imported, so a test can prove the mechanism and
    a future product can replace a different set without editing this.
    """
    path = build(tmp_path, {"web/index.html": b"x"})
    assert inspect(path, ("web",))
    with pytest.raises(ArchiveError, match="may not replace"):
        inspect(path, ALLOW)


# --- decompression bombs -------------------------------------------------

def test_a_highly_compressible_entry_is_refused(tmp_path):
    """
    The manifest pins the zip's byte count, and that is honest - a bomb is
    small. What it cannot pin is what the zip expands to, so the ratio is
    checked here.
    """
    path = build(tmp_path, {"app/bomb.bin": b"\0" * (MAX_RATIO * 5000),
                            "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="decompression bomb"):
        inspect(path, ALLOW)


def test_an_entry_too_large_even_uncompressed_is_refused(tmp_path):
    import jetpace_core.update.archive as arch
    original = arch.MAX_UNCOMPRESSED
    try:
        arch.MAX_UNCOMPRESSED = 64
        path = build(tmp_path, {"app/big.bin": b"abcdefgh" * 100,
                                "version.json": b"{}"},
                     method=zipfile.ZIP_STORED)
        with pytest.raises(ArchiveError, match="over the|expands to more"):
            inspect(path, ALLOW)
    finally:
        arch.MAX_UNCOMPRESSED = original


def test_too_many_entries_is_refused(tmp_path, monkeypatch):
    import jetpace_core.update.archive as arch
    monkeypatch.setattr(arch, "MAX_ENTRIES", 3)
    path = build(tmp_path, {f"app/f{i}.py": b"x" for i in range(5)})
    with pytest.raises(ArchiveError, match="over the"):
        inspect(path, ALLOW)


# --- malformed and hostile archives --------------------------------------

def test_something_that_is_not_a_zip_is_refused(tmp_path):
    path = tmp_path / "nope.zip"
    path.write_bytes(b"this is not a zip file at all")
    with pytest.raises(ArchiveError, match="not a readable zip"):
        inspect(path, ALLOW)


def test_a_truncated_zip_is_refused(tmp_path):
    path = good(tmp_path)
    data = path.read_bytes()
    path.write_bytes(data[:len(data) // 2])
    with pytest.raises(ArchiveError, match="not a readable zip"):
        inspect(path, ALLOW)


def test_a_corrupted_entry_fails_its_checksum(tmp_path):
    """
    A download whose bytes changed in flight must not be written out. The
    manifest's SHA-256 catches this first in the real flow; the CRC inside
    the zip is the second line.
    """
    path = build(tmp_path, {"app/main.py": b"A" * 4000, "version.json": b"{}"},
                 method=zipfile.ZIP_STORED)
    data = bytearray(path.read_bytes())
    # Flip bytes inside the stored payload, leaving the structure intact.
    start = data.find(b"A" * 100)
    assert start > 0
    for i in range(start, start + 50):
        data[i] = 0x42
    path.write_bytes(bytes(data))
    with pytest.raises(ArchiveError, match="checksum"):
        extract(path, tmp_path / "staging", ALLOW)


def test_an_empty_archive_is_refused(tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w"):
        pass
    with pytest.raises(ArchiveError, match="no files"):
        inspect(path, ALLOW)


def test_an_archive_of_only_directories_is_refused(tmp_path):
    path = tmp_path / "dirs.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("app/", b"")
        z.writestr("app/sub/", b"")
    with pytest.raises(ArchiveError, match="no files"):
        inspect(path, ALLOW)


def test_an_encrypted_entry_is_refused():
    """
    An encrypted release is not something to prompt about; it is not ours.

    The guard is exercised directly because `writestr` resets flag_bits, so
    zipfile cannot produce this archive - a real one would come from
    somewhere else.
    """
    from jetpace_core.update.archive import _check_member

    info = zipfile.ZipInfo("app/main.py")
    info.flag_bits |= 0x1
    info.file_size = 10
    info.compress_size = 10
    with pytest.raises(ArchiveError, match="encrypted"):
        _check_member(info, "app/main.py")


def test_an_unsupported_compression_method_is_refused(tmp_path):
    path = tmp_path / "bz.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("app/main.py", b"x" * 100)
        z.writestr("version.json", b"{}")
    with pytest.raises(ArchiveError, match="compression method"):
        inspect(path, ALLOW)


def test_a_symlink_entry_is_refused(tmp_path):
    """
    A zip can carry a symlink in the high bits of external_attr. One
    pointing at C:\\Windows\\System32 is not something to discover by
    trying to create it.
    """
    import stat as st
    attr = (st.S_IFLNK | 0o777) << 16
    path = build(tmp_path, {"app/link": (b"C:/Windows/System32", attr),
                            "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="symbolic link"):
        inspect(path, ALLOW)


def test_two_entries_differing_only_in_case_are_refused(tmp_path):
    """
    On Windows these are one file, so which one lands depends on the order
    they happen to be written. A release must not depend on that.
    """
    path = build(tmp_path, {"app/Main.py": b"a", "app/main.py": b"b",
                            "version.json": b"{}"})
    with pytest.raises(ArchiveError, match="twice"):
        inspect(path, ALLOW)


# --- version.json inside the release -------------------------------------

def test_a_release_without_a_version_file_reports_none(tmp_path):
    path = build(tmp_path, {"app/main.py": b"x"})
    assert version_in(path, ALLOW) is None


@pytest.mark.parametrize("content", [b"not json", b"{}", b'{"version": null}',
                                     b'{"version": ""}', b'{"version": 1.1}'])
def test_an_unreadable_version_file_is_refused(tmp_path, content):
    path = build(tmp_path, {"app/main.py": b"x", "version.json": content})
    with pytest.raises(ArchiveError, match="version"):
        version_in(path, ALLOW)


# --- top_of ---------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("app/main.py", "app"),
    ("app/sub/deep/thing.py", "app"),
    ("version.json", "version.json"),
    ("app", "app"),
])
def test_top_of(name, expected):
    assert top_of(name) == expected
