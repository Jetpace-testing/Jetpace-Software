"""
The release manifest contract.

Signing is the whole security boundary of the update mechanism: a site
installs a release if and only if this code says the signature is good. So
the interesting tests are not the happy path - they are every way a
manifest can be wrong, and the proof that each one is refused.
"""

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jetpace_core.update.manifest import (
    Manifest,
    ManifestError,
    canonical,
    compare_versions,
    is_newer,
    sign_manifest,
    verify_manifest,
)

GOOD = {
    "version": "1.2.3",
    "zip": "jetpace-1.2.3.zip",
    "sha256": "a" * 64,
    "bytes": 4096,
    "notes": "Fixes the pressure decode.",
    "published_at": "2026-10-12T09:00:00+00:00",
}


@pytest.fixture
def keys():
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


@pytest.fixture
def signed(keys):
    private, _ = keys
    return sign_manifest(GOOD, private)


# --- the happy path, briefly ----------------------------------------------

def test_a_signed_manifest_verifies(keys, signed):
    _, public = keys
    m = verify_manifest(json.loads(json.dumps(signed.__dict__)), public)
    assert m.version == "1.2.3"
    assert m.sha256 == "a" * 64
    assert m.bytes == 4096


def test_signing_then_verifying_survives_json(keys, signed):
    """A manifest goes over HTTP as JSON; it must survive the round trip."""
    _, public = keys
    as_json = json.dumps(signed.__dict__)
    assert verify_manifest(json.loads(as_json), public).zip == GOOD["zip"]


# --- the refusals, at length ----------------------------------------------

def test_a_manifest_signed_with_another_key_is_refused(signed):
    """
    The case that matters most. Anybody can publish a release; only the
    holder of the private key can publish one a site accepts.
    """
    attacker = Ed25519PrivateKey.generate()
    forged = sign_manifest(GOOD, attacker)
    with pytest.raises(ManifestError, match="does not match the release key"):
        verify_manifest(forged.__dict__, Ed25519PrivateKey.generate().public_key())


def test_tampering_with_the_hash_breaks_the_signature(keys, signed):
    """
    The zip's hash is inside the signed bytes, so swapping the zip for
    another one means re-signing - which needs the private key.
    """
    _, public = keys
    d = dict(signed.__dict__)
    d["sha256"] = "b" * 64
    with pytest.raises(ManifestError, match="does not match the release key"):
        verify_manifest(d, public)


@pytest.mark.parametrize("field", list(GOOD))
def test_tampering_with_any_signed_field_is_caught(keys, signed, field):
    """Every field in _ORDER is covered by the signature. Prove it, one by one."""
    _, public = keys
    d = dict(signed.__dict__)
    d[field] = 9999 if isinstance(d[field], int) else "tampered"
    if field == "version":
        d[field] = "9.9.9"
    elif field == "zip":
        d[field] = "other.zip"
    elif field == "sha256":
        d[field] = "c" * 64
    with pytest.raises(ManifestError):
        verify_manifest(d, public)


@pytest.mark.parametrize("missing", list(GOOD) + ["signature"])
def test_a_missing_field_is_refused(keys, signed, missing):
    _, public = keys
    d = dict(signed.__dict__)
    del d[missing]
    with pytest.raises(ManifestError):
        verify_manifest(d, public)


@pytest.mark.parametrize("version", ["1.2", "1.2.3.4", "v1.2.3", "latest", ""])
def test_a_version_that_is_not_x_y_z_is_refused(keys, version):
    private, public = keys
    m = sign_manifest({**GOOD, "version": version} if version else GOOD, private)
    d = dict(m.__dict__)
    d["version"] = version
    with pytest.raises(ManifestError):
        verify_manifest(d, public)


@pytest.mark.parametrize("name", [
    "../../evil.zip",            # a path, not a name
    "sub/dir/x.zip",
    "C:\\windows\\x.zip",
    "x.exe",                     # not a zip at all
    "x.zip.exe",
])
def test_a_zip_name_that_is_a_path_is_refused(keys, name):
    """
    The zip name becomes a file name in update/downloads/. A name
    containing a separator or '..' is how a release writes outside its own
    folder, so it is refused before anything is downloaded.
    """
    private, public = keys
    signed = sign_manifest({**GOOD, "zip": name}, private)
    with pytest.raises(ManifestError, match="not a plain file name"):
        verify_manifest(signed.__dict__, public)


@pytest.mark.parametrize("sha", [
    "A" * 64,          # uppercase: two spellings of one hash is one too many
    "a" * 63,
    "a" * 65,
    "z" * 64,
    "",
])
def test_a_malformed_hash_is_refused(keys, sha):
    private, public = keys
    signed = sign_manifest({**GOOD, "sha256": sha}, private)
    d = dict(signed.__dict__)
    d["sha256"] = sha
    with pytest.raises(ManifestError):
        verify_manifest(d, public)


@pytest.mark.parametrize("size", [0, -1, 1.5, "4096", True, None])
def test_a_bad_size_is_refused(keys, size):
    """
    bytes is checked against the download before the hash is computed, so a
    zero or a string here would skip that check. True is in the list
    because bool is an int in Python and 1 byte is not a release.
    """
    private, public = keys
    signed = sign_manifest(GOOD, private)
    d = dict(signed.__dict__)
    d["bytes"] = size
    with pytest.raises(ManifestError):
        verify_manifest(d, public)


@pytest.mark.parametrize("raw", [None, [], "a string", 42, True])
def test_something_that_is_not_an_object_is_refused(keys, raw):
    _, public = keys
    with pytest.raises(ManifestError, match="not an object"):
        verify_manifest(raw, public)


def test_a_signature_that_is_not_base64_is_refused(keys, signed):
    _, public = keys
    d = dict(signed.__dict__)
    d["signature"] = "not base64 !!!"
    with pytest.raises(ManifestError, match="not base64"):
        verify_manifest(d, public)


def test_an_empty_signature_is_refused(keys, signed):
    _, public = keys
    d = dict(signed.__dict__)
    d["signature"] = ""
    with pytest.raises(ManifestError):
        verify_manifest(d, public)


# --- the canonical bytes --------------------------------------------------

def test_canonical_bytes_have_no_incidental_whitespace():
    """
    Python's json.dumps puts a space after ':' and ',' by default and
    Node's JSON.stringify does not. One byte of difference is a failed
    signature, and the symptom - "signature does not match" - points at the
    key rather than at the formatting. Pin the exact bytes.
    """
    assert canonical(GOOD) == (
        b'{"version":"1.2.3","zip":"jetpace-1.2.3.zip","sha256":"'
        + b"a" * 64
        + b'","bytes":4096,"notes":"Fixes the pressure decode.",'
        b'"published_at":"2026-10-12T09:00:00+00:00"}'
    )


def test_canonical_order_does_not_follow_the_input_order():
    """A manifest from JSON may arrive with its keys in any order."""
    shuffled = {k: GOOD[k] for k in reversed(list(GOOD))}
    assert canonical(shuffled) == canonical(GOOD)


def test_signing_refuses_an_incomplete_manifest(keys):
    private, _ = keys
    with pytest.raises(ManifestError, match="missing"):
        sign_manifest({"version": "1.0.0"}, private)


# --- versions -------------------------------------------------------------

@pytest.mark.parametrize("a, b, expected", [
    ("1.0.0", "1.0.0", 0),
    ("1.0.1", "1.0.0", 1),
    ("1.0.0", "1.0.1", -1),
    ("1.10.0", "1.9.0", 1),      # not a string comparison
    ("2.0.0", "1.99.99", 1),
    ("1.0.0", "0.9.9", 1),
])
def test_version_comparison(a, b, expected):
    assert compare_versions(a, b) == expected


def test_is_newer_refuses_the_same_version():
    """
    An equal version is not an update. Re-installing the running version
    would stage a swap, restart the site and change nothing.
    """
    assert not is_newer("1.0.0", "1.0.0")
    assert is_newer("1.0.1", "1.0.0")
    assert not is_newer("0.9.9", "1.0.0")
