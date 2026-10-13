"""
Generate the release key, and sign releases with it.

    uv run python scripts/make_release.py --init-key
    uv run python scripts/make_release.py --from dist/app --version 1.1.0 --notes "..."
    uv run python scripts/make_release.py --verify build/jetpace-1.1.0.manifest.json

The key is the whole mechanism. A site installs a release if and only if
its manifest carries a signature from the matching private key, so:

  - The private key never enters this repository. It is written to
    .local/release-key.pem, which is gitignored, and that is a DEVELOPMENT
    key. The real one belongs offline, in two documented copies.
  - Losing the private key means no site can ever be updated again. There
    is no recovery path by design, because any recovery path is also an
    attack.
  - Rotating the key is a release like any other: the new public key ships
    inside a release signed by the old one.

This script is release tooling and never runs on a site.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

from cryptography.hazmat.primitives import serialization              # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (        # noqa: E402
    Ed25519PrivateKey,
)

from jetpace_core.update.layout import REPLACEABLE                     # noqa: E402
from jetpace_core.update.manifest import (                             # noqa: E402
    ManifestError,
    sign_manifest,
    verify_manifest,
)

PRIVATE_KEY = ROOT / ".local" / "release-key.pem"
KEY_MODULE = ROOT / "packages" / "core" / "src" / "jetpace_core" / "update" / "key.py"
BUILD = ROOT / "build"


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def public_spki_b64(private: Ed25519PrivateKey) -> str:
    import base64
    der = private.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode("ascii")


def init_key(force: bool = False) -> None:
    if PRIVATE_KEY.exists() and not force:
        die(f"{PRIVATE_KEY.relative_to(ROOT)} already exists.\n\n"
            "  Overwriting it would orphan every site running a build that\n"
            "  trusts the current key - they would refuse all future\n"
            "  releases. Pass --force only if you are certain no install\n"
            "  depends on it.")

    private = Ed25519PrivateKey.generate()
    PRIVATE_KEY.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_KEY.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    spki = public_spki_b64(private)

    say(f"private key written to {PRIVATE_KEY.relative_to(ROOT)}  (gitignored)")
    print()
    print("  Paste this into packages/core/src/jetpace_core/update/key.py:")
    print()
    print(f'    RELEASE_PUBLIC_KEY_SPKI = "{spki}"')
    print()
    print("  This is a DEVELOPMENT key. Before the first customer install,")
    print("  generate the real one on a machine that is not this laptop, keep")
    print("  the private half offline in two places, and write down who holds")
    print("  them. Losing it means no site can ever be updated again.")
    print()


def load_private() -> Ed25519PrivateKey:
    if not PRIVATE_KEY.exists():
        die(f"no signing key at {PRIVATE_KEY.relative_to(ROOT)}.\n\n"
            "  Generate one:\n"
            "    uv run python scripts/make_release.py --init-key")
    key = serialization.load_pem_private_key(PRIVATE_KEY.read_bytes(),
                                             password=None)
    if not isinstance(key, Ed25519PrivateKey):
        die("the signing key is not an Ed25519 private key")
    return key


def build_release(source: Path, version: str, notes: str) -> None:
    if not source.is_dir():
        die(f"{source} is not a directory")

    # A release zip contains the replaceable things and nothing else. This
    # is checked here, at build time, as well as on the site before
    # unpacking - a release that would be refused is better caught now.
    present = [n for n in REPLACEABLE if (source / n).exists()]
    if not present:
        die(f"{source} contains none of {REPLACEABLE}; nothing to release")
    strays = [p.name for p in source.iterdir() if p.name not in REPLACEABLE]
    if strays:
        die(f"{source} contains things a release may not replace: "
            f"{', '.join(sorted(strays))}.\n\n"
            f"  A release may carry only {', '.join(REPLACEABLE)}.")

    private = load_private()
    BUILD.mkdir(parents=True, exist_ok=True)
    zip_name = f"jetpace-{version}.zip"
    zip_path = BUILD / zip_name

    # The version inside the release must match the manifest. The site
    # checks this too; disagreement means a mis-tagged build.
    version_file = source / "version.json"
    version_file.write_text(json.dumps({"version": version}, indent=2) + "\n",
                            encoding="utf-8")

    say(f"packing {source} -> {zip_path.relative_to(ROOT)}")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(source).as_posix())

    data = zip_path.read_bytes()
    unsigned = {
        "version": version,
        "zip": zip_name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "notes": notes,
        "published_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "+00:00"),
    }
    manifest = sign_manifest(unsigned, private)

    manifest_path = BUILD / f"jetpace-{version}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.__dict__, indent=2) + "\n", encoding="utf-8")

    say(f"sha256 {unsigned['sha256']}")
    say(f"{len(data):,} bytes")
    say(f"manifest {manifest_path.relative_to(ROOT)}")
    print()
    print("  Publish BOTH files in the same release. The manifest is read")
    print("  first; the zip is only downloaded if its signature checks out.")
    print()


def verify_file(path: Path) -> None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"could not read {path}: {exc}")
    try:
        m = verify_manifest(raw)
    except ManifestError as exc:
        die(f"REFUSED: {exc}")
    say(f"signature is good: version {m.version}, {m.bytes:,} bytes")

    zip_path = path.parent / m.zip
    if not zip_path.exists():
        say(f"note: {m.zip} is not beside the manifest, so its hash was "
            f"not checked")
        return
    data = zip_path.read_bytes()
    if len(data) != m.bytes:
        die(f"{m.zip} is {len(data):,} bytes; the manifest says {m.bytes:,}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != m.sha256:
        die(f"{m.zip} does not match the signed hash")
    say(f"{m.zip} matches the signed hash")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init-key", action="store_true",
                    help="generate a signing key")
    ap.add_argument("--force", action="store_true",
                    help="with --init-key: overwrite an existing key")
    ap.add_argument("--from", dest="source", type=Path,
                    help="directory to release (must contain app/)")
    ap.add_argument("--version", help="x.y.z")
    ap.add_argument("--notes", default="", help="what changed")
    ap.add_argument("--verify", type=Path, metavar="MANIFEST",
                    help="check a manifest against the built-in public key")
    args = ap.parse_args()

    if args.init_key:
        return init_key(force=args.force)
    if args.verify:
        return verify_file(args.verify)
    if args.source or args.version:
        if not (args.source and args.version):
            die("--from and --version are both needed")
        return build_release(args.source, args.version, args.notes)
    ap.print_help()


if __name__ == "__main__":
    main()
