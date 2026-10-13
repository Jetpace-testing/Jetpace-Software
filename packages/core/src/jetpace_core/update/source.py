"""
Where releases come from.

A source is asked for the newest manifest and, if the site wants it, the
zip named in that manifest. It is never asked for anything else, and
nothing it returns is trusted: the manifest goes through
manifest.verify_manifest() and the zip through archive.inspect() before
either is acted on.

Two implementations:

    GitHubSource   the real one - GitHub Releases on a private repository,
                   read with a token that can do nothing but read
    DirSource      a folder of manifests and zips. What the tests use, and
                   what a site with no internet uses: copy two files onto
                   a USB stick, press Install

Ported from Meril RTD's src/update/source.ts, with one substantive change,
described at _NoAuthAcrossHosts below: Node drops the Authorization header
on a cross-host redirect and Python does not, so it has to be done by hand.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Protocol

from .manifest import SEMVER, compare_versions

USER_AGENT = "jetpace-updater"
API_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 15 * 60.0
CHUNK = 256 * 1024

# A release of the application is tens of megabytes. This is the ceiling on
# what will be written to disk at all, independent of what the manifest
# claims - the manifest is checked first, but a source that answers with
# something enormous should not be able to fill the disk while being
# refused.
MAX_DOWNLOAD = 512 * 1024 * 1024


class SourceError(Exception):
    """A source that could not be read, with a reason fit to show an admin."""


class UpdateSource(Protocol):
    name: str

    def probe(self) -> None:
        """Raise SourceError if the source cannot be read with what it has."""

    def latest(self) -> tuple[dict, str] | None:
        """The newest manifest as parsed JSON and where it came from."""

    def download(self, zip_name: str, dest: Path, expected_bytes: int,
                 on_bytes: Callable[[int], None] | None = None) -> None:
        """Fetch the named zip to dest."""


# ---------------------------------------------------------------------------

class _NoAuthAcrossHosts(urllib.request.HTTPRedirectHandler):
    """
    Drop the Authorization header when a redirect leaves the host.

    GitHub answers an asset download with a 302 to a signed URL on
    objects.githubusercontent.com or S3. The signature in that URL is the
    credential; the Authorization header is not needed there, and sending
    it hands our GitHub token to a third party that has no business
    holding it - where it lands in their access logs.

    urllib forwards every header except content-length and content-type
    across a redirect, including to a different host. Node's fetch drops
    Authorization on a cross-site redirect, which is why the TypeScript
    original has nothing like this class and a direct port would have been
    quietly worse than the thing it was ported from.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if _host_of(newurl) != _host_of(req.full_url):
            for name in list(new.headers):
                if name.lower() == "authorization":
                    del new.headers[name]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def _host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoAuthAcrossHosts())


class GitHubSource:
    """
    GitHub Releases on a private repository. The manifest is small and is
    fetched through the API; the zip is fetched through the API too, which
    redirects to a signed download URL.
    """

    def __init__(self, repo: str, token: str | Callable[[], str] = "") -> None:
        self.repo = repo
        self.name = f"github.com/{repo}"
        self._token = token
        self._last_assets: list[dict] | None = None

    def _token_now(self) -> str:
        t = self._token() if callable(self._token) else self._token
        return (t or "").strip()

    def _headers(self, accept: str) -> dict[str, str]:
        h = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._token_now()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _api(self, path: str, accept: str = "application/vnd.github+json",
             timeout: float = API_TIMEOUT):
        url = f"https://api.github.com{path}"
        req = urllib.request.Request(url, headers=self._headers(accept))
        try:
            return _opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SourceError(
                    f"GitHub refused the token ({exc.code}); check it on the "
                    f"update panel") from exc
            if exc.code == 404:
                raise SourceError(
                    "GitHub cannot find the repository or a release; is the "
                    "token for the right repository?") from exc
            raise SourceError(f"GitHub answered {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SourceError(f"could not reach GitHub: {exc}") from exc

    def probe(self) -> None:
        # The repository itself, not a release: a token is good or bad
        # whether or not anything has been published yet.
        with self._api(f"/repos/{self.repo}") as r:
            r.read(1)

    def latest(self) -> tuple[dict, str] | None:
        with self._api(f"/repos/{self.repo}/releases/latest") as r:
            try:
                release = json.loads(r.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise SourceError(
                    f"GitHub's answer was not JSON: {exc}") from exc

        assets = release.get("assets") or []
        self._last_assets = assets
        tag = release.get("tag_name", "?")

        manifests = [a for a in assets
                     if str(a.get("name", "")).endswith(".manifest.json")]
        if not manifests:
            return None
        asset = manifests[0]
        with self._api(f"/repos/{self.repo}/releases/assets/{asset['id']}",
                       accept="application/octet-stream") as r:
            body = r.read(1024 * 1024)      # a manifest is well under 1 MB
        try:
            return json.loads(body.decode("utf-8")), f"release {tag}"
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceError(
                f"the manifest in release {tag} is not JSON: {exc}") from exc

    def download(self, zip_name: str, dest: Path, expected_bytes: int,
                 on_bytes: Callable[[int], None] | None = None) -> None:
        assets = self._last_assets
        if assets is None:
            self.latest()
            assets = self._last_assets or []
        match = [a for a in assets if a.get("name") == zip_name]
        if not match:
            raise SourceError(f"the release has no asset named {zip_name}")

        response = self._api(
            f"/repos/{self.repo}/releases/assets/{match[0]['id']}",
            accept="application/octet-stream", timeout=DOWNLOAD_TIMEOUT)
        _stream_to(response, dest, expected_bytes, on_bytes)


class DirSource:
    """
    A folder holding `*.manifest.json` files and their zips; the newest
    version wins. What the tests use, and what a site without internet can
    use with a USB stick.
    """

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.name = str(self.dir)

    def probe(self) -> None:
        if not self.dir.is_dir():
            raise SourceError(f"{self.dir} is not there")

    def latest(self) -> tuple[dict, str] | None:
        if not self.dir.is_dir():
            return None
        best: tuple[str, Path] | None = None
        for path in self.dir.glob("*.manifest.json"):
            stem = path.name[:-len(".manifest.json")]
            version = stem.rsplit("-", 1)[-1]
            if not SEMVER.match(version):
                continue
            if best is None or compare_versions(version, best[0]) > 0:
                best = (version, path)
        if best is None:
            return None
        try:
            return json.loads(best[1].read_text(encoding="utf-8")), str(best[1])
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            raise SourceError(f"could not read {best[1]}: {exc}") from exc

    def download(self, zip_name: str, dest: Path, expected_bytes: int,
                 on_bytes: Callable[[int], None] | None = None) -> None:
        # zip_name has already been checked by verify_manifest to be a plain
        # file name, but this is the place it becomes a path, so it is
        # checked again here rather than trusted at a distance.
        if "/" in zip_name or "\\" in zip_name or zip_name in (".", ".."):
            raise SourceError(f"{zip_name!r} is not a plain file name")
        source = self.dir / zip_name
        if not source.is_file():
            raise SourceError(f"{source} is not there")
        size = source.stat().st_size
        if size != expected_bytes:
            raise SourceError(
                f"{zip_name} is {size:,} bytes; the manifest says "
                f"{expected_bytes:,}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        if on_bytes:
            on_bytes(size)


# ---------------------------------------------------------------------------

def _stream_to(response, dest: Path, expected_bytes: int,
               on_bytes: Callable[[int], None] | None) -> None:
    """
    Write a response body to dest, stopping if it is longer than the
    manifest said. The hash is checked afterwards, but a source that
    answers with an endless stream should not be able to fill the disk
    before getting there.
    """
    if expected_bytes > MAX_DOWNLOAD:
        raise SourceError(
            f"the manifest claims {expected_bytes:,} bytes, over the "
            f"{MAX_DOWNLOAD:,} limit")
    limit = min(expected_bytes, MAX_DOWNLOAD)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with response, dest.open("wb") as out:
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise SourceError(
                        f"the download is longer than the {limit:,} bytes "
                        f"the manifest promised; it was not kept")
                out.write(chunk)
                if on_bytes:
                    on_bytes(written)
    except (urllib.error.URLError, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise SourceError(f"the download failed: {exc}") from exc
    except SourceError:
        dest.unlink(missing_ok=True)
        raise

    if written != expected_bytes:
        dest.unlink(missing_ok=True)
        raise SourceError(
            f"the download is {written:,} bytes; the manifest says "
            f"{expected_bytes:,}")
