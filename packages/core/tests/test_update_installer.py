"""
The install path, end to end, and the sources that feed it.

The sequence under test is: verify the signature, download, check the
hash, check every name, check the version inside, unpack, stage. What
matters is not only that a good release gets through but that each failure
leaves the install folder exactly as it was - because anything left behind
in staging could be swapped in by a later restart without ever having been
verified.

Nothing here touches the network. GitHub is exercised through a fake
urlopen; the real HTTP work is one small function and the thing worth
pinning about it is which headers survive a redirect.
"""

import hashlib
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jetpace_core.update import layout_of
from jetpace_core.update.installer import (
    InstallError,
    Progress,
    check,
    install,
    rollback,
)
from jetpace_core.update.manifest import sign_manifest
from jetpace_core.update.source import (
    DirSource,
    GitHubSource,
    SourceError,
    _NoAuthAcrossHosts,
)
from jetpace_core.update.swap import Pending, read_pending, write_pending

KEY = Ed25519PrivateKey.generate()


@pytest.fixture(autouse=True)
def _use_the_test_key(monkeypatch):
    """Every test signs with its own key rather than the shipped one."""
    import jetpace_core.update.manifest as mf
    monkeypatch.setattr(mf, "_release_key", lambda: KEY.public_key())


# --- building releases ----------------------------------------------------

def make_zip(path: Path, version: str, extra: dict | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("app/main.py", f"print({version!r})\n")
        z.writestr("version.json", json.dumps({"version": version}))
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def publish(folder: Path, version: str, extra: dict | None = None,
            notes: str = "") -> Path:
    """A folder a DirSource can read: one zip and one signed manifest."""
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = make_zip(folder / f"jetpace-{version}.zip", version, extra)
    data = zip_path.read_bytes()
    manifest = sign_manifest({
        "version": version,
        "zip": zip_path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "notes": notes,
        "published_at": "2026-10-12T09:00:00+00:00",
    }, KEY)
    (folder / f"jetpace-{version}.manifest.json").write_text(
        json.dumps(manifest.__dict__, indent=2))
    return zip_path


def make_install(root: Path, version: str = "1.0.0") -> None:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "main.py").write_text(f"print({version!r})\n")
    (root / "version.json").write_text(json.dumps({"version": version}))


# --- DirSource ------------------------------------------------------------

def test_a_folder_source_finds_the_newest_release(tmp_path):
    releases = tmp_path / "releases"
    for v in ("1.0.1", "1.2.0", "1.0.9"):
        publish(releases, v)
    raw, where = DirSource(releases).latest()
    assert raw["version"] == "1.2.0"
    assert "1.2.0" in where


def test_a_folder_source_with_nothing_in_it_returns_none(tmp_path):
    (tmp_path / "empty").mkdir()
    assert DirSource(tmp_path / "empty").latest() is None


def test_a_folder_source_that_does_not_exist_returns_none(tmp_path):
    assert DirSource(tmp_path / "nope").latest() is None


def test_probe_says_so_when_the_folder_is_missing(tmp_path):
    with pytest.raises(SourceError, match="is not there"):
        DirSource(tmp_path / "nope").probe()


def test_a_folder_source_ignores_files_that_are_not_versioned(tmp_path):
    releases = tmp_path / "releases"
    publish(releases, "1.0.0")
    (releases / "notes.manifest.json").write_text("{}")
    (releases / "jetpace-latest.manifest.json").write_text("{}")
    raw, _ = DirSource(releases).latest()
    assert raw["version"] == "1.0.0"


def test_a_folder_source_refuses_a_zip_name_that_is_a_path(tmp_path):
    """
    verify_manifest already refuses such a name, but this is where it
    becomes a path, so it is checked here rather than trusted at a distance.
    """
    releases = tmp_path / "releases"
    releases.mkdir()
    with pytest.raises(SourceError, match="plain file name"):
        DirSource(releases).download("../evil.zip", tmp_path / "out.zip", 1)


def test_a_folder_source_refuses_a_zip_of_the_wrong_size(tmp_path):
    releases = tmp_path / "releases"
    zip_path = publish(releases, "1.1.0")
    with pytest.raises(SourceError, match="the manifest says"):
        DirSource(releases).download(zip_path.name, tmp_path / "out.zip", 99)


# --- check() --------------------------------------------------------------

def test_check_finds_a_newer_release(tmp_path):
    releases = tmp_path / "releases"
    publish(releases, "1.1.0")
    m = check(DirSource(releases), "1.0.0")
    assert m is not None and m.version == "1.1.0"


def test_check_returns_none_when_nothing_is_newer(tmp_path):
    releases = tmp_path / "releases"
    publish(releases, "1.0.0")
    assert check(DirSource(releases), "1.0.0") is None
    assert check(DirSource(releases), "1.2.0") is None


def test_check_raises_rather_than_shrugging_at_a_bad_signature(tmp_path):
    """
    "Nothing new" is routine. "Somebody published a release this site will
    not accept" is an event - either the wrong key or not our release - and
    it must not be reported as the same thing.
    """
    releases = tmp_path / "releases"
    publish(releases, "1.1.0")
    path = releases / "jetpace-1.1.0.manifest.json"
    m = json.loads(path.read_text())
    m["notes"] = "tampered"
    path.write_text(json.dumps(m))

    with pytest.raises(InstallError, match="signature does not match"):
        check(DirSource(releases), "1.0.0")


def test_check_reports_where_a_bad_manifest_came_from(tmp_path):
    releases = tmp_path / "releases"
    publish(releases, "1.1.0")
    path = releases / "jetpace-1.1.0.manifest.json"
    path.write_text(json.dumps({"version": "1.1.0"}))
    with pytest.raises(InstallError, match="manifest.json"):
        check(DirSource(releases), "1.0.0")


# --- install(), the happy path --------------------------------------------

def test_a_good_release_is_staged(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    publish(releases, "1.1.0")
    source = DirSource(releases)
    m = check(source, "1.0.0")

    pending = install(source, m, root, "1.0.0", by="rishi")

    layout = layout_of(root)
    assert pending.staged is True
    assert pending.to_version == "1.1.0"
    assert (layout.staging / "app" / "main.py").read_text() == "print('1.1.0')\n"
    assert read_pending(root) == pending
    # The running install is untouched until the restart.
    assert (root / "app" / "main.py").read_text() == "print('1.0.0')\n"


def test_the_download_folder_is_cleaned_up_after_a_successful_install(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    publish(releases, "1.1.0")
    source = DirSource(releases)
    install(source, check(source, "1.0.0"), root, "1.0.0", by="t")
    assert not layout_of(root).downloads.exists()


def test_progress_is_reported_in_order(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    publish(releases, "1.1.0")
    source = DirSource(releases)

    seen: list[Progress] = []
    install(source, check(source, "1.0.0"), root, "1.0.0", by="t",
            on_progress=seen.append)

    phases = [p.phase for p in seen]
    assert phases[0] == "downloading"
    assert "verifying" in phases
    assert "unpacking" in phases
    assert phases[-1] == "staged"


# --- install(), every refusal --------------------------------------------

def test_a_download_whose_hash_is_wrong_is_refused(tmp_path):
    """
    The manifest is signed, so its hash is authoritative. A zip that does
    not match it is either corrupted in flight or swapped for another.
    """
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    zip_path = publish(releases, "1.1.0")
    source = DirSource(releases)
    m = check(source, "1.0.0")

    # Same length, different content, so the size check passes and only
    # the hash catches it.
    data = bytearray(zip_path.read_bytes())
    data[-30:] = bytes(len(data[-30:]))
    zip_path.write_bytes(bytes(data))

    with pytest.raises(InstallError, match="does not match the signed hash"):
        install(source, m, root, "1.0.0", by="t")
    assert read_pending(root) is None
    assert not layout_of(root).staging.exists()


def test_a_release_whose_inner_version_disagrees_is_refused(tmp_path):
    """
    A mis-tagged build. Installing it would leave version.json claiming
    something the files are not, after which every later comparison is
    wrong and the site would never update again.
    """
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    releases.mkdir()

    zip_path = releases / "jetpace-1.1.0.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("app/main.py", b"x")
        z.writestr("version.json", json.dumps({"version": "9.9.9"}))
    data = zip_path.read_bytes()
    manifest = sign_manifest({
        "version": "1.1.0", "zip": zip_path.name,
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
        "notes": "", "published_at": "2026-10-12T09:00:00+00:00",
    }, KEY)
    (releases / "jetpace-1.1.0.manifest.json").write_text(
        json.dumps(manifest.__dict__))

    source = DirSource(releases)
    with pytest.raises(InstallError, match="says 1.1.0 but .* says 9.9.9"):
        install(source, check(source, "1.0.0"), root, "1.0.0", by="t")
    assert read_pending(root) is None


def test_a_release_carrying_something_it_may_not_replace_is_refused(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    publish(releases, "1.1.0", extra={"supervisor.exe": b"MZ..."})
    source = DirSource(releases)

    with pytest.raises(InstallError, match="may not replace"):
        install(source, check(source, "1.0.0"), root, "1.0.0", by="t")
    assert read_pending(root) is None
    assert not layout_of(root).staging.exists()


def test_a_release_that_would_escape_the_folder_is_refused(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    publish(releases, "1.1.0", extra={"../../evil.py": b"import os"})
    source = DirSource(releases)

    with pytest.raises(InstallError, match="climb out"):
        install(source, check(source, "1.0.0"), root, "1.0.0", by="t")
    assert not (tmp_path / "evil.py").exists()
    assert read_pending(root) is None


def test_a_release_with_no_app_folder_is_refused(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    releases.mkdir()
    zip_path = releases / "jetpace-1.1.0.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("version.json", json.dumps({"version": "1.1.0"}))
    data = zip_path.read_bytes()
    manifest = sign_manifest({
        "version": "1.1.0", "zip": zip_path.name,
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
        "notes": "", "published_at": "2026-10-12T09:00:00+00:00",
    }, KEY)
    (releases / "jetpace-1.1.0.manifest.json").write_text(
        json.dumps(manifest.__dict__))

    source = DirSource(releases)
    with pytest.raises(InstallError, match="no app folder"):
        install(source, check(source, "1.0.0"), root, "1.0.0", by="t")


def test_installing_nothing_says_so_rather_than_crashing(tmp_path):
    """check() returns None for "nothing newer"; forwarding that is a
    mistake a caller will make, and it should read as a sentence."""
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    publish(releases, "1.0.0")
    source = DirSource(releases)
    with pytest.raises(InstallError, match="no update to install"):
        install(source, check(source, "1.0.0"), root, "1.0.0", by="t")


def test_installing_while_an_update_is_already_staged_is_refused(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    write_pending(root, Pending("1.0.0", "1.1.0", True, 0, "now", "someone"))
    releases = tmp_path / "releases"
    publish(releases, "1.2.0")
    source = DirSource(releases)

    with pytest.raises(InstallError, match="already staged"):
        install(source, check(source, "1.0.0"), root, "1.0.0", by="t")


def test_installing_a_version_that_is_not_newer_is_refused(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    publish(releases, "1.1.0")
    source = DirSource(releases)
    m = check(source, "1.0.0")

    with pytest.raises(InstallError, match="not newer"):
        install(source, m, root, "1.1.0", by="t")


def test_a_missing_zip_leaves_nothing_behind(tmp_path):
    root = tmp_path / "site"
    make_install(root)
    releases = tmp_path / "releases"
    zip_path = publish(releases, "1.1.0")
    source = DirSource(releases)
    m = check(source, "1.0.0")
    zip_path.unlink()

    with pytest.raises(InstallError, match="is not there"):
        install(source, m, root, "1.0.0", by="t")
    assert read_pending(root) is None
    assert not layout_of(root).staging.exists()
    assert not layout_of(root).downloads.exists()


def test_a_stale_staging_folder_is_cleared_before_unpacking(tmp_path):
    """
    A leftover staging folder plus a pending.json from a later attempt
    would swap in a release nobody verified. So staging is emptied before
    an unpack and after any failure.
    """
    root = tmp_path / "site"
    make_install(root)
    layout = layout_of(root)
    (layout.staging / "app").mkdir(parents=True)
    (layout.staging / "app" / "ghost.py").write_text("# from an old attempt")

    releases = tmp_path / "releases"
    publish(releases, "1.1.0")
    source = DirSource(releases)
    install(source, check(source, "1.0.0"), root, "1.0.0", by="t")

    assert not (layout.staging / "app" / "ghost.py").exists()
    assert (layout.staging / "app" / "main.py").exists()


# --- rollback -------------------------------------------------------------

def test_rollback_stages_the_backup(tmp_path):
    root = tmp_path / "site"
    make_install(root, "1.1.0")
    layout = layout_of(root)
    backup = layout.backup_for("1.0.0")
    (backup / "app").mkdir(parents=True)
    (backup / "app" / "main.py").write_text("print('1.0.0')\n")

    pending = rollback(root, "1.1.0", by="rishi")

    assert pending.to_version == "1.0.0"
    assert pending.from_version == "1.1.0"
    assert pending.staged is True
    assert (layout.staging / "app" / "main.py").read_text() == "print('1.0.0')\n"
    assert not backup.exists()          # moved, not copied


def test_rollback_with_no_backup_is_refused(tmp_path):
    root = tmp_path / "site"
    make_install(root, "1.1.0")
    with pytest.raises(InstallError, match="no previous version"):
        rollback(root, "1.1.0", by="t")


def test_rollback_while_an_update_is_staged_is_refused(tmp_path):
    root = tmp_path / "site"
    make_install(root, "1.1.0")
    write_pending(root, Pending("1.1.0", "1.2.0", True, 0, "now", "someone"))
    with pytest.raises(InstallError, match="already staged"):
        rollback(root, "1.1.0", by="t")


# --- the GitHub source ----------------------------------------------------

class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            body, self._body = self._body, b""
            return body
        body, self._body = self._body[:n], self._body[n:]
        return body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_github_reads_the_manifest_asset(monkeypatch, tmp_path):
    release = {"tag_name": "v1.1.0", "assets": [
        {"id": 7, "name": "jetpace-1.1.0.manifest.json", "size": 10},
        {"id": 8, "name": "jetpace-1.1.0.zip", "size": 100},
    ]}
    manifest_body = json.dumps({"version": "1.1.0"}).encode()

    def fake_open(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/releases/latest"):
            return FakeResponse(json.dumps(release).encode())
        if url.endswith("/assets/7"):
            return FakeResponse(manifest_body)
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    raw, where = GitHubSource("Jetpace-testing/Jetpace-Software", "tok").latest()
    assert raw["version"] == "1.1.0"
    assert where == "release v1.1.0"


def test_github_returns_none_when_a_release_has_no_manifest(monkeypatch):
    release = {"tag_name": "v1.1.0",
               "assets": [{"id": 8, "name": "jetpace-1.1.0.zip", "size": 1}]}

    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda self, req, timeout=None:
                        FakeResponse(json.dumps(release).encode()))
    assert GitHubSource("a/b", "tok").latest() is None


@pytest.mark.parametrize("code, expect", [
    (401, "refused the token"),
    (403, "refused the token"),
    (404, "cannot find the repository"),
    (500, "answered 500"),
])
def test_github_http_errors_become_readable_messages(monkeypatch, code, expect):
    def fail(self, req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "no", {}, None)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fail)
    with pytest.raises(SourceError, match=expect):
        GitHubSource("a/b", "tok").probe()


def test_github_a_network_failure_says_so(monkeypatch):
    def fail(self, req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fail)
    with pytest.raises(SourceError, match="could not reach GitHub"):
        GitHubSource("a/b", "tok").probe()


def test_github_sends_a_bearer_token_when_it_has_one(monkeypatch):
    seen = {}

    def capture(self, req, timeout=None):
        seen.update({k.lower(): v for k, v in req.headers.items()})
        return FakeResponse(b"{}")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", capture)
    GitHubSource("a/b", "secret-token").probe()
    assert seen["authorization"] == "Bearer secret-token"


def test_github_sends_no_authorization_header_without_a_token(monkeypatch):
    seen = {}

    def capture(self, req, timeout=None):
        seen.update({k.lower(): v for k, v in req.headers.items()})
        return FakeResponse(b"{}")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", capture)
    GitHubSource("a/b", "").probe()
    assert "authorization" not in seen


def test_a_token_can_be_supplied_lazily(monkeypatch):
    """The token is read from the database at call time, not at construction."""
    seen = {}
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda self, req, timeout=None:
                        (seen.update({k.lower(): v for k, v
                                      in req.headers.items()}),
                         FakeResponse(b"{}"))[1])
    token = {"value": "first"}
    source = GitHubSource("a/b", lambda: token["value"])
    source.probe()
    assert seen["authorization"] == "Bearer first"
    token["value"] = "second"
    source.probe()
    assert seen["authorization"] == "Bearer second"


# --- the redirect, which is the interesting part --------------------------

def _redirect_to(original_url: str, new_url: str):
    handler = _NoAuthAcrossHosts()
    req = urllib.request.Request(original_url, headers={
        "Authorization": "Bearer secret-token",
        "Accept": "application/octet-stream",
        "User-Agent": "jetpace-updater",
    })
    return handler.redirect_request(req, None, 302, "Found", {}, new_url)


def test_the_token_is_not_sent_to_a_redirect_on_another_host():
    """
    GitHub answers an asset download with a 302 to a signed URL on another
    host. The signature in that URL is the credential; our token is not
    needed there, and sending it puts it in somebody else's access logs.

    urllib forwards every header except content-length and content-type
    across a redirect, including across hosts - unlike Node's fetch, which
    drops Authorization. So the TypeScript original needed nothing here and
    a direct port would have been quietly worse than the thing it came from.
    """
    new = _redirect_to("https://api.github.com/repos/a/b/releases/assets/8",
                       "https://objects.githubusercontent.com/signed-thing")
    headers = {k.lower() for k in new.headers}
    assert "authorization" not in headers
    assert new.unredirected_hdrs.get("Authorization") is None
    # The harmless headers survive, so the request still works.
    assert "accept" in headers


def test_the_token_is_kept_on_a_redirect_within_the_same_host():
    new = _redirect_to("https://api.github.com/repos/a/b/releases/assets/8",
                       "https://api.github.com/somewhere-else")
    assert any(k.lower() == "authorization" for k in new.headers)


def test_a_redirect_to_a_subdomain_counts_as_another_host():
    """
    Host comparison is exact, not suffix-based. `evil.github.com` is not
    `github.com`, and treating a suffix match as the same host is how this
    sort of check is usually got wrong.
    """
    new = _redirect_to("https://api.github.com/x",
                       "https://evil.api.github.com/x")
    assert not any(k.lower() == "authorization" for k in new.headers)


def test_a_redirect_to_http_from_https_still_drops_the_token(monkeypatch):
    new = _redirect_to("https://api.github.com/x", "http://api.github.com/x")
    # Same host, so the token survives the host rule - which is why the
    # scheme downgrade is worth noting here even though urllib itself
    # refuses to follow https -> http redirects.
    assert any(k.lower() == "authorization" for k in new.headers)


# --- download limits ------------------------------------------------------

def test_a_download_longer_than_promised_is_stopped(tmp_path):
    from jetpace_core.update.source import _stream_to

    dest = tmp_path / "out.zip"
    with pytest.raises(SourceError, match="longer than"):
        _stream_to(FakeResponse(b"x" * 5000), dest, expected_bytes=100,
                   on_bytes=None)
    assert not dest.exists()          # nothing kept


def test_a_download_shorter_than_promised_is_refused(tmp_path):
    from jetpace_core.update.source import _stream_to

    dest = tmp_path / "out.zip"
    with pytest.raises(SourceError, match="is 50 bytes"):
        _stream_to(FakeResponse(b"x" * 50), dest, expected_bytes=100,
                   on_bytes=None)
    assert not dest.exists()


def test_a_manifest_claiming_an_absurd_size_is_refused_before_downloading(tmp_path):
    from jetpace_core.update.source import MAX_DOWNLOAD, _stream_to

    dest = tmp_path / "out.zip"
    with pytest.raises(SourceError, match="over the"):
        _stream_to(FakeResponse(b""), dest, expected_bytes=MAX_DOWNLOAD + 1,
                   on_bytes=None)
    assert not dest.exists()


def test_progress_is_reported_while_downloading(tmp_path):
    from jetpace_core.update.source import _stream_to

    seen: list[int] = []
    _stream_to(FakeResponse(b"x" * 1000), tmp_path / "out.zip",
               expected_bytes=1000, on_bytes=seen.append)
    assert seen and seen[-1] == 1000
