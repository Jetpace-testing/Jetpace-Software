"""
Passwords, tokens and the arithmetic of not getting them wrong.

Deliberately small, deliberately boring, and deliberately free of any
dependency: `hashlib.scrypt` is in the standard library, so this adds
nothing to the install and nothing to the licence register. Meril RTD
does the same thing in Node's crypto, and this is a transcription of its
reasoning rather than a new design.

Nothing here touches a socket, a database or a clock except
`time.monotonic` in the throttle, so all of it is testable without any
of those.

WHAT IS AND IS NOT A SECRET HERE. A password never leaves this module
except as a hash. A session token is generated here, returned ONCE to
the caller to put in a cookie, and stored only as a SHA-256 digest - so
a database dump, a backup, a support export yields nothing anybody can
sign in with.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import unicodedata

# ---------------------------------------------------------------------------
# password hashing
#
# scrypt, with the cost PARAMETERS STORED IN THE HASH rather than as
# constants the verifier assumes. That is what makes it possible to raise
# the cost later: an old hash still verifies with the parameters it was
# written with, and `needs_rehash` says when to replace it on the next
# successful sign-in. A verifier with hardcoded parameters means the cost
# can never change without invalidating every password on the site.
#
# N = 2^16, r = 8, p = 1 measured at about 1.1 s and 64 MB on the
# development machine, which is a slow laptop. A second is a great deal
# to spend on a login and nothing at all to spend once a shift; it is the
# figure an attacker with a stolen database pays per guess, per account,
# for ever.
#
# OWASP's floor for scrypt is 2^17/8/1 or an equivalent work factor. This
# is one notch below it, chosen because a plant PC is slower than this
# one and a four-second login is a thing people route around. If the
# machines get faster, raise LOG_N: every existing hash keeps working.
# ---------------------------------------------------------------------------

LOG_N = 16
R = 8
P = 1
SALT_BYTES = 16
KEY_BYTES = 32

# scrypt needs 128 * N * r bytes. Python refuses above `maxmem`, whose
# default is 32 MB - too small for these parameters, and the resulting
# ValueError would look like a bug rather than a setting.
MAXMEM = 128 * (2 ** LOG_N) * R * 2

SCHEME = "scrypt"


def hash_password(password: str) -> str:
    """
    `scrypt$logN$r$p$salt$key`, all base64url, no padding.

    A fresh random salt every time, so two people with the same password
    have nothing in common in the table.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, LOG_N, R, P)
    return "$".join([SCHEME, str(LOG_N), str(R), str(P),
                     _b64(salt), _b64(key)])


def verify_password(password: str, stored: str | None) -> bool:
    """
    Constant-time, and False for anything malformed rather than raising.

    `stored` is None for an account that has no password - disabled, or
    one that signs in through an identity provider. That is a legitimate
    state and not an error, so it returns False like any other failure.

    THE COMPARISON IS hmac.compare_digest. A plain `==` on bytes leaves
    the timing of the comparison proportional to how many leading bytes
    matched, which is enough to recover a hash a byte at a time given
    enough attempts. The throttle makes that impractical; this makes it
    impossible, and costs nothing.
    """
    if not stored:
        return False
    try:
        scheme, log_n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != SCHEME:
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(key_b64)
        actual = _derive(password, salt, int(log_n), int(r), int(p),
                         dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str | None) -> bool:
    """
    Whether this hash was written with weaker parameters than current.

    Called after a SUCCESSFUL verification, which is the only moment the
    plaintext is in hand to rehash with. Raising LOG_N therefore upgrades
    the site one sign-in at a time, with nobody being asked to do
    anything.
    """
    if not stored:
        return False
    try:
        scheme, log_n, r, p, _salt, _key = stored.split("$")
    except ValueError:
        return True
    if scheme != SCHEME:
        return True
    return (int(log_n), int(r), int(p)) < (LOG_N, R, P)


def _derive(password: str, salt: bytes, log_n: int, r: int, p: int,
            dklen: int = KEY_BYTES) -> bytes:
    # NFKC first. "é" can be typed as one code point or as "e" plus a
    # combining accent, and the two are different bytes - so a password
    # set on one keyboard layout would not verify when typed on another.
    # Normalising both sides makes them the same password, which is what
    # the person typing it believes.
    material = unicodedata.normalize("NFKC", password).encode("utf-8")
    return hashlib.scrypt(material, salt=salt, n=2 ** log_n, r=r, p=p,
                          dklen=dklen, maxmem=MAXMEM)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# session tokens
# ---------------------------------------------------------------------------

TOKEN_BYTES = 32            # 256 bits from secrets.token_urlsafe


def new_token() -> str:
    """A session token. Returned once; never stored in this form."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """
    What the database holds instead of the token.

    Plain SHA-256 and NOT a slow KDF, deliberately. A token is 256 bits
    of `secrets` output, so there is no dictionary to run against it and
    nothing for a work factor to buy - while this runs on every single
    request, where a 1-second hash would be the whole response time.
    Passwords are low-entropy and rare; tokens are high-entropy and
    constant. They get opposite treatment for that reason.
    """
    return hashlib.sha256(token.encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# password policy
#
# §11 requires "a password policy" and does not say which. This one
# follows NIST SP 800-63B: LENGTH over composition, and a check against
# what is obviously guessable. No "one capital, one digit, one symbol" -
# that rule reliably produces Password1! and a sticky note, and NIST
# withdrew it in 2017.
#
# No forced expiry either, for the same reason: rotation drives people to
# predictable increments. `password_changed_at` is recorded so a site
# whose inspector insists can add aging without a migration, which is the
# honest way to hold that position - the mechanism exists, the default
# does not punish people.
# ---------------------------------------------------------------------------

MIN_LENGTH = 12
MAX_LENGTH = 256            # bounds the work scrypt will be asked to do

# Not a dictionary - a dictionary belongs in a file and would be a
# dependency. These are the shapes that actually turn up on industrial
# systems, which is a different and much shorter list.
OBVIOUS = frozenset({
    "password", "passw0rd", "letmein", "welcome", "qwerty", "admin",
    "administrator", "changeme", "jetpace", "cleanroom", "pharma",
    "123456", "12345678", "123456789", "1234567890", "iloveyou",
    "monkey", "dragon", "abc123", "qwertyuiop", "11111111", "00000000",
})


class PasswordRejected(Exception):
    """A password refused, with a sentence saying what to do instead."""


def check_password(password: str, username: str = "",
                   full_name: str = "") -> None:
    """
    Raise `PasswordRejected` if this password may not be used.

    Returns None when it is fine. The username and name are taken so the
    password cannot simply be them - which is the single most common
    thing people pick when a system asks for twelve characters and says
    nothing else.
    """
    if password != password.strip():
        raise PasswordRejected(
            "The password starts or ends with a space. That is allowed by "
            "some systems and not by others, and you will not be able to "
            "tell which one refused you.")
    if len(password) < MIN_LENGTH:
        raise PasswordRejected(
            f"Use at least {MIN_LENGTH} characters. Length is what makes a "
            f"password hard to guess — a long phrase beats a short one "
            f"with symbols in it.")
    if len(password) > MAX_LENGTH:
        raise PasswordRejected(
            f"That is longer than {MAX_LENGTH} characters.")

    folded = _fold(password)
    if folded in OBVIOUS:
        raise PasswordRejected(
            "That is one of the first passwords anybody tries.")
    for part in _identity_parts(username, full_name):
        if part and part in folded:
            raise PasswordRejected(
                "The password contains your own name or username, which is "
                "the first thing somebody guessing would try.")
    if len(set(password)) < 5:
        raise PasswordRejected(
            "That is the same few characters repeated. Length only helps "
            "when the characters differ.")

    # A FIXED LIST CANNOT CATCH THESE, which is why they are rules rather
    # than entries. "123456789012" is twelve characters with ten distinct
    # ones and is not in any list short enough to keep in a source file;
    # it is also among the first things anybody tries. Both of these are
    # about SHAPE, and shape is what a list of literals is bad at.
    if password.isdigit():
        raise PasswordRejected(
            "An all-numeric password is a PIN, and people pick dates. Use "
            "words — a phrase of four ordinary ones is far harder to "
            "guess and far easier to remember.")
    if _is_a_run(folded):
        raise PasswordRejected(
            "That is a straight run along the keyboard or the alphabet.")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


# The three rows of a QWERTY keyboard, which is what "a run" means to
# somebody's hands rather than to the alphabet.
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "abcdefghijklmnopqrstuvwxyz",
         "0123456789")


def _is_a_run(folded: str) -> bool:
    """
    Whether the whole password is a straight walk along a keyboard row,
    the alphabet or the digits - forwards or backwards.

    The WHOLE of it, not a part: "fghjkl" inside a passphrase is a
    coincidence, and rejecting on a substring would refuse perfectly good
    passwords for a reason their owner cannot see.
    """
    if len(folded) < 4:
        return False
    for row in _ROWS:
        if folded in row or folded in row[::-1]:
            return True
    return False


def _identity_parts(username: str, full_name: str) -> list[str]:
    parts = [_fold(username)]
    parts += [_fold(p) for p in re.split(r"[\s._-]+", full_name) if len(p) > 2]
    return [p for p in parts if len(p) > 2]


# ---------------------------------------------------------------------------
# throttling
#
# TWO LAYERS, because they stop different attacks.
#
#   PER ACCOUNT, in the database (app_user.failed_logins, locked_until).
#   Stops someone working through a password list against one known
#   username. It is in the database and not in memory because a process
#   restart must not clear a lockout - on a site where anybody can reach
#   the supervisor, that would be the attack.
#
#   PER ADDRESS, in memory, which is this class. Stops someone trying one
#   common password against every username on the site - "password
#   spraying" - which never trips a per-account counter because no
#   account sees more than one attempt. In memory is right here: the
#   address is not an identity, the value expires in seconds, and
#   persisting it would mean writing a row on every failed login, which
#   is itself a way to fill a disk.
#
# Taken from RTD, which has the per-address half.
# ---------------------------------------------------------------------------

MAX_FAILURES = 5
LOCKOUT_SECONDS = 30.0
ACCOUNT_MAX_FAILURES = 10
ACCOUNT_LOCKOUT_SECONDS = 900.0      # fifteen minutes


class Throttle:
    """Failed sign-ins per address. Not thread-safe by design; see below."""

    def __init__(self, max_failures: int = MAX_FAILURES,
                 lockout_s: float = LOCKOUT_SECONDS) -> None:
        self.max_failures = max_failures
        self.lockout_s = lockout_s
        # A dict mutated from FastAPI's thread pool. CPython's dict
        # operations are atomic under the GIL, and the worst a race can
        # do here is lose one failure count - which is a worse outcome
        # for the attacker than for us, and much cheaper than a lock on
        # the login path.
        self._failures: dict[str, tuple[int, float]] = {}

    def blocked_for(self, address: str) -> int:
        """Seconds this address must wait, or 0."""
        count, until = self._failures.get(address, (0, 0.0))
        remaining = until - time.monotonic()
        return max(0, int(remaining + 0.999)) if remaining > 0 else 0

    def fail(self, address: str) -> None:
        count, until = self._failures.get(address, (0, 0.0))
        # A lockout that has run out starts the count again rather than
        # continuing it, so one wrong password a day never accumulates
        # into a block.
        if until and until <= time.monotonic():
            count = 0
        count += 1
        self._failures[address] = (
            count,
            time.monotonic() + self.lockout_s if count >= self.max_failures
            else 0.0,
        )

    def clear(self, address: str) -> None:
        self._failures.pop(address, None)

    def forget_expired(self, now: float | None = None) -> int:
        """
        Drop entries nobody is waiting on.

        Called on the login path rather than from a timer: the dict only
        grows when sign-ins fail, so the place that grows it is the right
        place to trim it, and there is no thread to get wrong.
        """
        now = time.monotonic() if now is None else now
        stale = [a for a, (_c, until) in self._failures.items()
                 if until and until <= now]
        for address in stale:
            self._failures.pop(address, None)
        return len(stale)
