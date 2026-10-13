"""
What a release says about itself, and whether to believe it.

A release publishes two files: the zip, and a manifest naming the zip and
its SHA-256, signed with the Jetpace release key. A site reads the manifest
first. If the signature does not check out it never downloads the zip; if
the zip's hash does not match it never unpacks it.

That ordering is the whole security property. Someone who takes over the
GitHub account - or GitHub itself - can publish a release. They cannot
publish one a site will accept, because they do not have the private key.

Ported from Meril RTD's src/update/manifest.ts. Ed25519 and the canonical
field order are unchanged, so a manifest signed by the RTD tooling verifies
here and vice versa.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

from .key import RELEASE_PUBLIC_KEY_SPKI

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
PLAIN_ZIP = re.compile(r"^[A-Za-z0-9._-]+\.zip$")


class ManifestError(Exception):
    """A manifest that must not be acted on, with the reason in the message."""


@dataclass(frozen=True)
class Manifest:
    version: str          # '1.0.6'; must match the version inside the zip
    zip: str              # file name of the zip asset in the same release
    sha256: str           # SHA-256 of the zip, lowercase hex
    bytes: int
    notes: str            # what changed, for the administrator to read
    published_at: str
    signature: str        # Ed25519 over canonical(), base64

    def unsigned(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("signature")
        return d


# The exact bytes that are signed. Both ends must agree on this or every
# signature fails, so the field order is fixed here and nowhere else.
# separators= matters: Python's json.dumps puts a space after ':' and ','
# by default, Node's JSON.stringify does not, and a single byte of
# difference is a failed signature.
_ORDER = ("version", "zip", "sha256", "bytes", "notes", "published_at")


def canonical(unsigned: dict[str, Any]) -> bytes:
    ordered = {k: unsigned[k] for k in _ORDER}
    return json.dumps(ordered, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _release_key() -> Ed25519PublicKey:
    if not RELEASE_PUBLIC_KEY_SPKI or RELEASE_PUBLIC_KEY_SPKI.startswith("<"):
        # Fail closed. An unset key must refuse every release, never accept
        # them all - the opposite mistake ships a build that installs
        # anything anyone publishes.
        raise ManifestError(
            "no release public key is set; this build cannot verify any "
            "release. Generate a key with scripts/make_release.py --init-key "
            "and paste the public half into update/key.py")
    der = base64.b64decode(RELEASE_PUBLIC_KEY_SPKI)
    key = load_der_public_key(der)
    if not isinstance(key, Ed25519PublicKey):
        raise ManifestError("the release key is not an Ed25519 public key")
    return key


def sign_manifest(unsigned: dict[str, Any], private_key) -> Manifest:
    """Used only by the release tooling, never on a site."""
    missing = [k for k in _ORDER if k not in unsigned]
    if missing:
        raise ManifestError(f"cannot sign; missing {', '.join(missing)}")
    sig = private_key.sign(canonical(unsigned))
    return Manifest(**{k: unsigned[k] for k in _ORDER},
                    signature=base64.b64encode(sig).decode("ascii"))


def verify_manifest(raw: Any, key: Ed25519PublicKey | None = None) -> Manifest:
    """
    Parse and check a manifest. Raises ManifestError saying why, rather than
    returning a bare False - every rejection here is something an
    administrator needs to be told, not a condition to branch on silently.

    Every field is validated before the signature, because the signature
    covers exactly these fields and a malformed one means the canonical
    bytes cannot even be reconstructed.
    """
    if not isinstance(raw, dict):
        raise ManifestError("manifest is not an object")

    def need_str(field: str) -> str:
        v = raw.get(field)
        if not isinstance(v, str) or not v:
            raise ManifestError(f"manifest is missing {field}")
        return v

    version = need_str("version")
    if not SEMVER.match(version):
        raise ManifestError(f"manifest version {version!r} is not x.y.z")

    zip_name = need_str("zip")
    if not PLAIN_ZIP.match(zip_name):
        # A name with a slash or '..' in it would be a path, and a path in a
        # download name is how a release writes outside its own folder.
        raise ManifestError(
            f"manifest zip name {zip_name!r} is not a plain file name")

    sha = need_str("sha256")
    if not SHA256_HEX.match(sha):
        raise ManifestError("manifest hash is not lowercase SHA-256 hex")

    size = raw.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ManifestError("manifest has no valid size")

    notes = raw.get("notes")
    if not isinstance(notes, str):
        raise ManifestError("manifest has no notes")

    published_at = need_str("published_at")
    signature = need_str("signature")

    unsigned = {"version": version, "zip": zip_name, "sha256": sha,
                "bytes": size, "notes": notes, "published_at": published_at}
    try:
        sig = base64.b64decode(signature, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ManifestError("manifest signature is not base64") from exc

    try:
        (key or _release_key()).verify(sig, canonical(unsigned))
    except InvalidSignature as exc:
        raise ManifestError(
            "manifest signature does not match the release key") from exc

    return Manifest(**unsigned, signature=signature)


def compare_versions(a: str, b: str) -> int:
    """-1, 0, 1 for a before, equal to, after b. Only x.y.z is understood."""
    pa = [int(x) for x in a.split(".")[:3]] + [0, 0, 0]
    pb = [int(x) for x in b.split(".")[:3]] + [0, 0, 0]
    for i in range(3):
        if pa[i] != pb[i]:
            return -1 if pa[i] < pb[i] else 1
    return 0


def is_newer(candidate: str, current: str) -> bool:
    return compare_versions(candidate, current) > 0
