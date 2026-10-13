"""
The public half of the release signing key.

The private half never enters this repository. It lives offline, in two
documented copies, and signs every manifest the release tooling publishes.

This constant is what makes an update button safe to hand a site: a release
is installed only if it was signed with the matching private key. Someone
with the GitHub password, or GitHub itself, can publish a release; they
cannot publish one a site will accept.

Rotating the key is a release like any other - the new key ships inside a
release signed by the old one. Which also means: losing the private key
means no site can ever be updated again. There is no recovery path, by
design, because any recovery path is also an attack.

Until a key is generated this is unset, and verify_manifest() refuses every
release rather than accepting them all. Fail closed.

    uv run python scripts/make_release.py --init-key
"""

# DEVELOPMENT KEY, generated 12 October 2026 on the build laptop. The
# private half is .local/release-key.pem, which is gitignored. This must
# be replaced before the first customer install - see the module
# docstring, and the open decision on key custody in architecture.md.
RELEASE_PUBLIC_KEY_SPKI = "MCowBQYDK2VwAyEAyUp/capXt++SOAbC3jmYS9pQggGUDVCzilP3i/x0hTA="
