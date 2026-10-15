"""
Passwords, tokens, the policy and the throttle.

No socket, no database, no clock except a monotonic one the throttle
takes. Slow on purpose in two places: scrypt at the real parameters costs
about a second, and the tests that need a genuine hash pay it rather than
testing a weakened one. Those are marked, and there are only three.
"""

import re
import time
from pathlib import Path

import pytest

from jetpace_core import permissions as P
from jetpace_core import security as S

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "migrations" / "0008_identity.sql"


# --- hashing --------------------------------------------------------------

def test_a_password_verifies_against_its_own_hash():
    stored = S.hash_password("correct horse battery staple")
    assert S.verify_password("correct horse battery staple", stored)


def test_a_wrong_password_does_not():
    stored = S.hash_password("correct horse battery staple")
    assert not S.verify_password("correct horse battery stapler", stored)


def test_two_people_with_the_same_password_share_nothing():
    """
    A fresh salt every time. Equal hashes would tell anybody holding the
    table which accounts to attack once, and reuse a single cracked
    result across all of them.
    """
    a = S.hash_password("the same password")
    b = S.hash_password("the same password")
    assert a != b
    assert S.verify_password("the same password", a)
    assert S.verify_password("the same password", b)


def test_the_hash_carries_its_own_cost_parameters():
    """
    What makes it possible to raise the cost later. A verifier with
    hardcoded parameters means the cost can never change without
    invalidating every password on the site.
    """
    scheme, log_n, r, p, salt, key = S.hash_password("x" * 12).split("$")
    assert scheme == "scrypt"
    assert (int(log_n), int(r), int(p)) == (S.LOG_N, S.R, S.P)
    assert len(S._unb64(salt)) == S.SALT_BYTES
    assert len(S._unb64(key)) == S.KEY_BYTES


def test_an_old_hash_still_verifies_after_the_cost_is_raised():
    """
    The upgrade path: an existing hash keeps working, and `needs_rehash`
    says to replace it on the next successful sign-in - the one moment
    the plaintext is in hand.
    """
    weak = "$".join(["scrypt", "14", "8", "1",
                     S._b64(b"0" * 16),
                     S._b64(S._derive("hunter2hunter2", b"0" * 16, 14, 8, 1))])
    assert S.verify_password("hunter2hunter2", weak)
    assert S.needs_rehash(weak)
    assert not S.needs_rehash(S.hash_password("hunter2hunter2"))


def test_an_account_with_no_password_cannot_be_signed_into():
    """
    Null is a legitimate state - disabled, or an account that signs in
    through an identity provider - so it is False rather than an error.
    """
    assert not S.verify_password("anything", None)
    assert not S.verify_password("", None)


@pytest.mark.parametrize("stored", [
    "", "not-a-hash", "scrypt$16$8", "bcrypt$16$8$1$aaaa$bbbb",
    "scrypt$x$8$1$aaaa$bbbb", "scrypt$16$8$1$!!!!$bbbb",
])
def test_a_malformed_hash_is_false_and_not_an_exception(stored):
    """
    A corrupt row must fail the sign-in, not 500 the login page - which
    would be a way to tell, from outside, that a particular account's row
    is damaged.
    """
    assert S.verify_password("anything", stored) is False


def test_the_same_password_typed_two_ways_is_the_same_password():
    """
    NFKC. "é" is one code point or two depending on the keyboard, and a
    password set on one layout has to verify when typed on another.
    """
    composed = "café au lait 12"
    decomposed = "café au lait 12"
    assert composed != decomposed
    assert S.verify_password(decomposed, S.hash_password(composed))


def test_verification_is_constant_time():
    """
    hmac.compare_digest, not ==. A plain comparison leaks how many
    leading bytes matched through its timing, which is enough to recover
    a hash a byte at a time.
    """
    source = Path(S.__file__).read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    assert not re.search(r"return\s+actual\s*==\s*expected", source)


def test_scrypt_is_given_enough_memory_to_run():
    """
    Python's default maxmem is 32 MB and these parameters need 64. The
    resulting ValueError would surface as a 500 on every login and look
    like anything but a setting.
    """
    assert S.MAXMEM > 128 * (2 ** S.LOG_N) * S.R


def test_the_work_factor_is_not_quietly_weakened():
    """
    Pinned by value. This number is what an attacker with a stolen
    database pays per guess, per account, for ever - and it is a one-line
    change that no test would otherwise notice.
    """
    assert (S.LOG_N, S.R, S.P) >= (16, 8, 1)


# --- tokens ---------------------------------------------------------------

def test_a_token_is_long_and_never_repeats():
    tokens = {S.new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 40 for t in tokens)


def test_the_database_holds_a_digest_and_not_the_token():
    token = S.new_token()
    digest = S.token_hash(token)
    assert token not in digest
    assert len(digest) == 64 and re.fullmatch(r"[0-9a-f]{64}", digest)


def test_hashing_a_token_is_fast():
    """
    SHA-256 and not a KDF, deliberately: this runs on EVERY request,
    where a one-second hash would be the whole response time. A token is
    256 bits of `secrets` output, so there is no dictionary for a work
    factor to defend against.
    """
    token = S.new_token()
    started = time.perf_counter()
    for _ in range(1000):
        S.token_hash(token)
    assert time.perf_counter() - started < 0.5


# --- the policy -----------------------------------------------------------

def test_a_long_passphrase_is_accepted():
    S.check_password("the bicycle shed is green", "rshah", "Rishi Shah")


@pytest.mark.parametrize("password", [
    "short", "elevenchar", "Passw0rd!",          # all under 12
])
def test_too_short_is_refused_whatever_is_in_it(password):
    with pytest.raises(S.PasswordRejected) as exc:
        S.check_password(password)
    assert "12 characters" in str(exc.value)


def test_composition_rules_are_not_imposed():
    """
    NIST SP 800-63B withdrew them in 2017: "one capital, one digit, one
    symbol" reliably produces Password1! and a sticky note. Length is the
    requirement.
    """
    S.check_password("aaa bbb ccc ddd eee")       # no digit, no symbol, no capital


def test_an_obvious_password_is_refused_even_when_long_enough():
    with pytest.raises(S.PasswordRejected):
        S.check_password("administrator")


@pytest.mark.parametrize("password, why", [
    ("123456789012", "numeric"),      # 12 chars, 10 distinct, in no list
    ("190419041904", "numeric"),      # a date, twice
    ("qwertyuiop",   "run"),
    ("abcdefghijklm", "run"),
    ("0987654321",   "run"),
])
def test_shapes_a_word_list_cannot_catch(password, why):
    """
    The reason these are RULES and not entries. "123456789012" is twelve
    characters with ten distinct ones, is in no list short enough to keep
    in a source file, and is among the first things anybody tries. A
    literal list is bad at shape.
    """
    with pytest.raises(S.PasswordRejected):
        S.check_password(password)


def test_a_run_inside_a_passphrase_is_a_coincidence_not_a_rejection():
    """
    Rejecting on a substring would refuse a good password for a reason
    its owner cannot see. Only the WHOLE password being a run counts.
    """
    S.check_password("the asdf cat sat down")


def test_a_password_cannot_be_the_persons_own_name():
    """
    The most common answer to "twelve characters and nothing else said".
    """
    with pytest.raises(S.PasswordRejected) as exc:
        S.check_password("RishiShah1704", "rshah", "Rishi Shah")
    assert "your own name" in str(exc.value)

    with pytest.raises(S.PasswordRejected):
        S.check_password("xx rshah xxxx", "rshah", "Rishi Shah")


def test_a_short_name_does_not_ban_every_password():
    """
    A two-letter surname inside a passphrase must not be a rejection, or
    a person called Li cannot choose anything.
    """
    S.check_password("the quick brown fox", "li", "Li Wu")


def test_repeated_characters_are_refused():
    with pytest.raises(S.PasswordRejected):
        S.check_password("ababababababab")


def test_leading_or_trailing_space_is_refused_rather_than_silently_kept():
    """
    Some systems trim and some do not. Accepting it means the person
    cannot tell which one refused them later.
    """
    with pytest.raises(S.PasswordRejected) as exc:
        S.check_password(" a good long passphrase ")
    assert "space" in str(exc.value)


def test_there_is_no_forced_expiry_by_default():
    """
    Rotation drives predictable increments. The COLUMN exists
    (password_changed_at) so a site whose inspector insists can add
    aging without a migration - which is the honest way to hold the
    position.
    """
    source = Path(S.__file__).read_text(encoding="utf-8")
    assert "MAX_AGE" not in source
    assert "password_changed_at" in MIGRATION.read_text(encoding="utf-8")


# --- the throttle ---------------------------------------------------------

def test_five_wrong_passwords_earn_a_wait():
    t = S.Throttle()
    for _ in range(4):
        t.fail("10.0.0.1")
    assert t.blocked_for("10.0.0.1") == 0
    t.fail("10.0.0.1")
    assert 0 < t.blocked_for("10.0.0.1") <= S.LOCKOUT_SECONDS


def test_one_address_blocking_does_not_block_another():
    t = S.Throttle()
    for _ in range(5):
        t.fail("10.0.0.1")
    assert t.blocked_for("10.0.0.2") == 0


def test_a_successful_sign_in_clears_the_count():
    t = S.Throttle()
    for _ in range(4):
        t.fail("10.0.0.1")
    t.clear("10.0.0.1")
    for _ in range(4):
        t.fail("10.0.0.1")
    assert t.blocked_for("10.0.0.1") == 0, "the count restarted"


def test_an_expired_lockout_starts_the_count_again():
    """
    Otherwise one wrong password a day accumulates into a block weeks
    later, which is indistinguishable from a broken login.
    """
    t = S.Throttle(max_failures=2, lockout_s=0.01)
    t.fail("a")
    t.fail("a")
    assert t.blocked_for("a") > 0
    time.sleep(0.02)
    assert t.blocked_for("a") == 0
    t.fail("a")
    assert t.blocked_for("a") == 0, "should be failure 1 of 2, not 3 of 2"


def test_the_throttle_forgets_addresses_nobody_is_waiting_on():
    """
    It is an in-memory dict on a public endpoint. Without trimming, an
    attacker from many addresses grows it without bound.
    """
    t = S.Throttle(max_failures=1, lockout_s=0.01)
    for i in range(50):
        t.fail(f"10.0.0.{i}")
    time.sleep(0.02)
    assert t.forget_expired() == 50
    assert t._failures == {}


def test_the_account_lockout_is_longer_than_the_address_one():
    """
    They defend different attacks. A per-address block stops a burst; a
    per-account lock stops a list being worked through one known
    username, and that one has to cost real time.
    """
    assert S.ACCOUNT_LOCKOUT_SECONDS >= 10 * S.LOCKOUT_SECONDS
    assert S.ACCOUNT_MAX_FAILURES > S.MAX_FAILURES


def test_the_account_lockout_survives_a_restart():
    """
    It is a database column, not a dict. A process restart must not be a
    way to clear a lockout - on a site where anybody can reach the
    supervisor, that would be the attack.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "failed_logins" in sql and "locked_until" in sql
