"""
Signing in, against a scripted database.

The first test in this file is the one that matters. Everything else
here is ordinary coverage; that one pins a defect that passed every unit
test, passed review, and was only visible by making eight wrong attempts
against a real install and then looking at the table.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jetpace_core import security
from jetpace_core.web import identity

NOW = datetime.now(timezone.utc)


class Cur:
    """A cursor that answers from a script and remembers the SQL."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Conn:
    def __init__(self, answers=()):
        self.cur = Cur(answers)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    @property
    def sql(self):
        return self.cur.sql


def a_user(**over):
    row = {
        "id": 7, "tenant_id": 1, "username": "rshah",
        "full_name": "Rishi Shah",
        "password_hash": None, "must_change_password": False,
        "disabled_at": None, "locked_until": None, "failed_logins": 0,
    }
    return {**row, **over}


# --- the one that matters -------------------------------------------------

def test_a_refused_sign_in_commits_what_it_recorded():
    """
    THE DEFECT THIS FILE EXISTS FOR, found by running it rather than by
    testing it.

    `_fail` writes an audit row and the caller has already incremented
    `failed_logins`. Then it raises - and the caller's
    `with queries.connect(...)` block sees the exception and ROLLS BACK,
    discarding both. The refusal undoes its own record of itself.

    Measured on the live dashboard before the fix: twelve wrong
    passwords left failed_logins at 0 and the audit table empty. So
    §11's requirement to detect and report attempts at unauthorised use
    was not met, and the per-account lockout could never fire however
    long somebody guessed.

    A refusal has to be its own transaction.
    """
    conn = Conn([{"id": 1}, {"id": 99, "at": NOW}])
    with pytest.raises(identity.SignInRejected):
        identity._fail(conn, None, "10.0.0.1", "rshah", "wrong password")
    assert conn.commits == 1, (
        "the audit row and the failure count must survive the exception "
        "that the refusal raises")
    assert any("INSERT INTO audit" in s for s in conn.sql)


def test_the_success_path_does_not_commit():
    """
    The other half of the same decision. A session row and its audit
    entry must land together or not at all, so the success path leaves
    the transaction to the caller - the same rule as writes.py.
    """
    source = Path(identity.__file__).read_text(encoding="utf-8")
    body = source.split("def sign_in(")[1].split("def _fail(")[0]
    assert "conn.commit()" not in body


# --- a failure tells you nothing ------------------------------------------

def test_every_refusal_gives_the_same_sentence():
    """
    "No such user" told apart from "wrong password" is a list of valid
    usernames, obtainable by anybody with a browser.
    """
    # Each path asks the database a different number of questions before
    # it refuses, so each gets the script its own path will consume.
    #   tenant lookup + audit insert are the last two in every case;
    #   the wrong-password path adds NOTE_FAILURE before them.
    tail = [{"id": 1}, {"id": 1, "at": NOW}]
    cases = [
        ("no such user", [None] + tail),
        ("account disabled", [a_user(disabled_at=NOW)] + tail),
        ("wrong password",
         [a_user(password_hash=security.hash_password("x" * 14)),
          {"failed_logins": 1, "locked_until": None}] + tail),
    ]

    reasons = []
    for expected, script in cases:
        conn = Conn(script)
        with pytest.raises(identity.SignInRejected) as exc:
            identity.sign_in(conn, username="rshah", password="nope nope nope")
        reasons.append(exc.value.reason)
        assert exc.value.message == identity.SAME_ANSWER, expected

    assert reasons == [c[0] for c in cases], (
        "the specific reason is kept for the audit row and the log, and "
        "only the generic message reaches the page")


def test_an_unknown_username_still_pays_for_a_hash(monkeypatch):
    """
    THE TIMING EQUALISER. Without it the response time answers "does
    this username exist", which is the first step of every attack on a
    login page.
    """
    paid = []
    monkeypatch.setattr(security, "verify_password",
                        lambda pw, stored: paid.append(stored) or False)
    conn = Conn([None, {"id": 1}, {"id": 1, "at": NOW}])
    with pytest.raises(identity.SignInRejected):
        identity.sign_in(conn, username="nobody", password="x")
    assert paid, "no verification was performed for an unknown user"


def test_the_equaliser_is_built_lazily_not_at_import():
    """
    It was at import, and on a machine short of memory the 64 MB scrypt
    allocation failed with "malloc failure" and took the whole web
    process down before it served one request. A dashboard that will not
    start, because of a hash nobody had asked for yet.
    """
    source = Path(identity.__file__).read_text(encoding="utf-8")
    assert "_absent_hash: str | None = None" in source
    assert not re.search(r"^_ABSENT_USER_HASH\s*=\s*security\.hash_password",
                         source, re.M)


def test_a_disabled_account_cannot_sign_in():
    conn = Conn([a_user(disabled_at=NOW), {"id": 1}, {"id": 1, "at": NOW}])
    with pytest.raises(identity.SignInRejected) as exc:
        identity.sign_in(conn, username="rshah", password="x" * 14)
    assert exc.value.reason == "account disabled"


def test_a_locked_account_is_refused_without_checking_the_password():
    conn = Conn([a_user(locked_until=NOW + timedelta(minutes=5)),
                 {"id": 1}, {"id": 1, "at": NOW}])
    with pytest.raises(identity.SignInRejected) as exc:
        identity.sign_in(conn, username="rshah", password="x" * 14)
    assert exc.value.reason == "account locked"


def test_the_address_throttle_refuses_before_touching_the_database():
    throttle = security.Throttle(max_failures=1, lockout_s=60)
    throttle.fail("10.0.0.1")
    conn = Conn([])
    with pytest.raises(identity.SignInRejected) as exc:
        identity.sign_in(conn, username="rshah", password="x",
                         ip="10.0.0.1", throttle=throttle)
    assert exc.value.retry_after > 0
    assert conn.sql == [], "it should not have looked the user up"


# --- sessions -------------------------------------------------------------

def test_resolving_a_session_can_be_asked_not_to_extend_it():
    """
    The automatic-logoff mechanism. A background poll reads the session
    and must not push the expiry forward, or a dashboard left open on a
    plant floor never signs itself out.
    """
    row = {"user_id": 7, "username": "rshah", "full_name": "Rishi Shah",
           "tenant_id": 1, "must_change_password": False,
           "disabled_at": None, "expires_at": NOW + timedelta(minutes=15),
           "token_hash": "x", "created_at": NOW, "last_seen_at": NOW}

    quiet = Conn([row])
    identity.resolve(quiet, "token", slide=False)
    assert not any("UPDATE session" in s for s in quiet.sql)

    busy = Conn([row])
    identity.resolve(busy, "token", slide=True)
    assert any("UPDATE session" in s for s in busy.sql)


def test_a_session_whose_account_was_disabled_ends_at_once():
    """
    Rather than working for another fifteen minutes. Somebody whose
    access was just removed should not keep a usable session.
    """
    conn = Conn([{"user_id": 7, "username": "r", "full_name": "R",
                  "tenant_id": 1, "must_change_password": False,
                  "disabled_at": NOW, "expires_at": NOW,
                  "token_hash": "x", "created_at": NOW, "last_seen_at": NOW}])
    assert identity.resolve(conn, "token") is None
    assert any("UPDATE session SET ended_at" in s for s in conn.sql)


def test_an_absent_token_resolves_to_nobody_without_a_query():
    conn = Conn([])
    assert identity.resolve(conn, None) is None
    assert identity.resolve(conn, "") is None
    assert conn.sql == []


def test_the_query_enforces_both_the_idle_and_the_absolute_limit():
    """
    An idle timer alone can be held open for ever by a browser tab, and
    a session outliving a shift attributes the next shift's actions to
    whoever signed in.
    """
    assert "expires_at > now()" in identity.RESOLVE_SESSION
    assert f"interval '{identity.ABSOLUTE_HOURS} hours'" \
        in identity.RESOLVE_SESSION
    assert "ended_at IS NULL" in identity.RESOLVE_SESSION


# --- the principal --------------------------------------------------------

def principal(*roles):
    return identity.Principal(
        user_id=1, username="rshah", full_name="Rishi Shah", tenant_id=1,
        site_roles={1: frozenset(roles)})


def test_a_permission_is_asked_about_a_site():
    me = principal("engineer")
    assert me.can("device.add", site_id=1)
    assert not me.can("device.add", site_id=2), (
        "a role is granted per site; holding it at site 1 says nothing "
        "about site 2")


def test_initials_come_from_the_name_and_fall_back_to_the_username():
    assert principal().initials == "RS"
    assert identity.Principal(
        user_id=1, username="ops", full_name="Operations",
        tenant_id=1).initials == "OP"


def test_the_header_label_is_the_strongest_role_held():
    assert principal("viewer", "administrator").label == "Administrator"
    assert principal("viewer").label == "Viewer"
    assert identity.Principal(user_id=1, username="x", full_name="X Y",
                             tenant_id=1).label == "No role"


# --- creating an account --------------------------------------------------

def test_a_username_has_to_be_usable_in_an_audit_log():
    from jetpace_core.web.writes import WriteRejected
    for bad in ("", "A", "has space", "x" * 40, "Capital"):
        with pytest.raises(WriteRejected):
            identity.create_user(Conn([]), username=bad, full_name="A B",
                                 password="a good long passphrase",
                                 roles=("viewer",))


def test_an_account_with_no_role_is_refused():
    from jetpace_core.web.writes import WriteRejected
    with pytest.raises(WriteRejected) as exc:
        identity.create_user(Conn([]), username="ab", full_name="A B",
                             password="a good long passphrase", roles=())
    assert "at least one role" in str(exc.value)


def test_a_password_change_ends_every_session_the_account_has():
    """
    A password changed because it may have been learned is worth nothing
    while the session opened with the old one keeps working.
    """
    source = Path(identity.__file__).read_text(encoding="utf-8")
    body = source.split("def set_password(")[1].split("def set_disabled(")[0]
    assert "end_all_for(" in body
