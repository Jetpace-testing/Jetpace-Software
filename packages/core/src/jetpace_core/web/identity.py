"""
Signing in, staying signed in, and signing out.

A third module beside `queries.py` (which only reads) and `writes.py`
(which only changes instrument configuration), because identity is
neither and splitting it across both would scatter one subject over two
files. Everything about who somebody is lives here.

WHAT THIS MODULE GUARANTEES, and what the tests hold it to:

  A FAILED SIGN-IN LOOKS THE SAME WHATEVER WAS WRONG. Same message, same
  work done, same time taken, whether the username exists or not. An
  unknown user still pays a full scrypt verification against a dummy
  hash - otherwise the response time tells anybody who asks which
  usernames are real, which is the first step of every attack on a login
  page.

  EVERY ATTEMPT IS RECORDED. §11 requires detecting and reporting
  attempts at unauthorised use. A success and a failure both write an
  audit row, and the audit table is append-only for everyone.

  THE TOKEN IS WRITTEN DOWN ONCE, in the browser's cookie. The database
  holds its SHA-256 and nothing else.

  A POLL IS NOT ACTIVITY. `resolve(..., slide=False)` reads a session
  without extending it. The overview refreshes four panels every five
  seconds; if that counted as the operator being present, the automatic
  logoff Annex 11 §12 requires would never once fire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import logging

import psycopg

from .. import permissions as perms
from .. import security
from .writes import WriteRejected, record

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# how long a session lives
#
# TWO LIMITS, because they stop different things.
#
#   IDLE - fifteen minutes since the last real interaction. This is the
#   automatic logoff of 21 CFR 11.10(d) and Annex 11 §12: a logged-in
#   workstation left unattended on a plant floor is an account anybody
#   walking past is using. Fifteen is the figure GxP installations
#   normally settle on.
#
#   ABSOLUTE - twelve hours since sign-in, whatever has happened since.
#   An idle timer alone can be held open for ever by a browser tab, and
#   a shift is twelve hours; a session that outlives the person's shift
#   is attributing the next shift's actions to them.
# ---------------------------------------------------------------------------

IDLE_MINUTES = 15
ABSOLUTE_HOURS = 12

COOKIE = "jetpace_session"

# A real scrypt hash of a value nobody knows, verified against whenever
# the username does not exist so that the work - and therefore the time -
# is the same either way.
#
# LAZY, AND NOT AT IMPORT. It was at import, and on a machine short of
# memory the 64 MB scrypt allocation failed with "malloc failure" and
# took the whole web process down before it served a single request - a
# dashboard that does not start, on a plant PC under load, because of a
# hash nobody had asked for yet. Computed on the first sign-in attempt
# instead, and cached for the life of the process.
_absent_hash: str | None = None


def _absent_user_hash() -> str | None:
    global _absent_hash
    if _absent_hash is None:
        try:
            _absent_hash = security.hash_password(security.new_token())
        except (ValueError, MemoryError) as exc:
            # The equaliser is best-effort. If the machine cannot spare
            # the memory, a sign-in for an unknown user returns faster
            # than one for a known user - which is a timing leak worth
            # logging and not worth refusing to run over.
            log.warning("could not build the timing equaliser: %s", exc)
            return None
    return _absent_hash


def _equalise(password: str) -> None:
    """Spend the same work as a real verification, and discard it."""
    reference = _absent_user_hash()
    if reference is not None:
        security.verify_password(password, reference)


class SignInRejected(Exception):
    """
    A sign-in refused.

    `message` is deliberately vague - "those details did not match" and
    never "no such user" or "wrong password". `reason` is the specific
    truth, for the audit row and the log, and must never reach the page.
    """

    def __init__(self, message: str, reason: str, retry_after: int = 0):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.retry_after = retry_after


SAME_ANSWER = "Those details did not match an account."


# ---------------------------------------------------------------------------
# who somebody is, once resolved
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    """
    The signed-in person, as every route sees them.

    Frozen, and built once per request. Roles are held PER SITE because
    §10 says a role is granted per site; `can()` therefore always asks
    about somewhere, and defaults to "anywhere" only for the single-site
    installs that are all of them today.
    """

    user_id: int
    username: str
    full_name: str
    tenant_id: int
    site_roles: dict[int, frozenset[str]] = field(default_factory=dict)
    must_change_password: bool = False
    expires_at: datetime | None = None

    @property
    def roles(self) -> frozenset[str]:
        """Every role held anywhere. For display, never for a decision."""
        out: set[str] = set()
        for held in self.site_roles.values():
            out |= held
        return frozenset(out)

    def roles_at(self, site_id: int | None) -> frozenset[str]:
        if site_id is None:
            return self.roles
        return self.site_roles.get(site_id, frozenset())

    def can(self, permission: str, site_id: int | None = None) -> bool:
        return perms.allowed(self.roles_at(site_id), permission)

    @property
    def label(self) -> str:
        """The highest-sounding role they hold, for the header."""
        held = self.roles
        for role in reversed(perms.ROLES):
            if role in held:
                return perms.ROLE_LABELS[role]
        return "No role"

    @property
    def initials(self) -> str:
        parts = [p for p in self.full_name.split() if p]
        if not parts:
            return self.username[:2].upper()
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

FIND_USER = """
SELECT u.id, u.tenant_id, u.username, u.full_name, u.password_hash,
       u.must_change_password, u.disabled_at, u.locked_until,
       u.failed_logins
FROM   app_user u
WHERE  u.tenant_id = (SELECT id FROM tenant ORDER BY id LIMIT 1)
  AND  lower(u.username) = lower(%(username)s)
"""

ROLES_OF = """
SELECT r.site_id, r.role
FROM   user_role r
WHERE  r.user_id = %(user_id)s
  AND  (r.expires_at IS NULL OR r.expires_at > now())
"""

COUNT_USERS = """
SELECT count(*) AS n
FROM   app_user
WHERE  disabled_at IS NULL
"""

RESOLVE_SESSION = f"""
SELECT s.token_hash, s.user_id, s.created_at, s.last_seen_at, s.expires_at,
       u.username, u.full_name, u.tenant_id, u.must_change_password,
       u.disabled_at
FROM   session s
JOIN   app_user u ON u.id = s.user_id
WHERE  s.token_hash = %(token_hash)s
  AND  s.ended_at IS NULL
  AND  s.expires_at > now()
  AND  s.created_at > now() - interval '{ABSOLUTE_HOURS} hours'
"""

CREATE_SESSION = """
INSERT INTO session (token_hash, user_id, expires_at, ip, user_agent)
VALUES (%(token_hash)s, %(user_id)s, %(expires_at)s, %(ip)s, %(user_agent)s)
"""

SLIDE_SESSION = """
UPDATE session
   SET last_seen_at = now(), expires_at = %(expires_at)s
 WHERE token_hash = %(token_hash)s AND ended_at IS NULL
"""

END_SESSION = """
UPDATE session SET ended_at = now(), ended_reason = %(reason)s
 WHERE token_hash = %(token_hash)s AND ended_at IS NULL
"""

END_USER_SESSIONS = """
UPDATE session SET ended_at = now(), ended_reason = %(reason)s
 WHERE user_id = %(user_id)s AND ended_at IS NULL
"""

NOTE_FAILURE = """
UPDATE app_user
   SET failed_logins = failed_logins + 1,
       locked_until = CASE WHEN failed_logins + 1 >= %(max_failures)s
                           THEN now() + %(lockout)s::interval
                           ELSE locked_until END
 WHERE id = %(user_id)s
RETURNING failed_logins, locked_until
"""

NOTE_SUCCESS = """
UPDATE app_user
   SET failed_logins = 0, locked_until = NULL, last_login_at = now()
 WHERE id = %(user_id)s
"""


def _expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=IDLE_MINUTES)


def count_users(conn: psycopg.Connection) -> int:
    """How many accounts can sign in. Zero means first run."""
    with conn.cursor() as cur:
        cur.execute(COUNT_USERS)
        return cur.fetchone()["n"]


def _roles_of(conn: psycopg.Connection, user_id: int) -> dict[int, frozenset[str]]:
    with conn.cursor() as cur:
        cur.execute(ROLES_OF, {"user_id": user_id})
        grouped: dict[int, set[str]] = {}
        for row in cur.fetchall():
            grouped.setdefault(row["site_id"], set()).add(row["role"])
    return {site: frozenset(roles) for site, roles in grouped.items()}


# ---------------------------------------------------------------------------
# signing in
# ---------------------------------------------------------------------------

def sign_in(conn: psycopg.Connection, *, username: str, password: str,
            ip: str = "", user_agent: str = "",
            throttle: security.Throttle | None = None) -> tuple[Principal, str]:
    """
    Check a password and open a session. Returns the principal and the
    token to put in the cookie - the only time that value exists.

    Raises `SignInRejected` for every failure, with the same message for
    all of them. Does not commit; the caller's connection context manager
    does.
    """
    username = (username or "").strip()

    if throttle is not None:
        throttle.forget_expired()
        waiting = throttle.blocked_for(ip)
        if waiting:
            raise SignInRejected(
                f"Too many attempts from this machine. Try again in "
                f"{waiting} seconds.", "address throttled", waiting)

    with conn.cursor() as cur:
        cur.execute(FIND_USER, {"username": username})
        user = cur.fetchone()

    # THE TIMING EQUALISER. An unknown username still pays a full scrypt
    # verification, so the response takes the same second either way.
    # Without this, a login page answers "does this username exist" to
    # anybody with a stopwatch.
    if user is None:
        _equalise(password)
        _fail(conn, throttle, ip, username, "no such user")

    if user["disabled_at"] is not None:
        _equalise(password)
        _fail(conn, throttle, ip, username, "account disabled",
              user_id=user["id"])

    locked = user["locked_until"]
    if locked is not None and locked > datetime.now(timezone.utc):
        seconds = int((locked - datetime.now(timezone.utc)).total_seconds())
        _fail(conn, throttle, ip, username, "account locked",
              user_id=user["id"])

    if not security.verify_password(password, user["password_hash"]):
        with conn.cursor() as cur:
            cur.execute(NOTE_FAILURE, {
                "user_id": user["id"],
                "max_failures": security.ACCOUNT_MAX_FAILURES,
                "lockout": f"{int(security.ACCOUNT_LOCKOUT_SECONDS)} seconds",
            })
            after = cur.fetchone()
        _fail(conn, throttle, ip, username, "wrong password",
              user_id=user["id"],
              detail={"failed_logins": after["failed_logins"],
                      "locked": after["locked_until"] is not None})

    # --- past this line the password was right ---------------------------

    if throttle is not None:
        throttle.clear(ip)

    # Raise the work factor on the one occasion the plaintext is in hand.
    if security.needs_rehash(user["password_hash"]):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app_user SET password_hash = %(hash)s WHERE id = %(id)s",
                {"hash": security.hash_password(password), "id": user["id"]})

    token = security.new_token()
    with conn.cursor() as cur:
        cur.execute(CREATE_SESSION, {
            "token_hash": security.token_hash(token),
            "user_id": user["id"],
            "expires_at": _expiry(),
            "ip": ip or None,
            "user_agent": (user_agent or None) and user_agent[:300],
        })
        cur.execute(NOTE_SUCCESS, {"user_id": user["id"]})

    record(conn, tenant_id=user["tenant_id"], action="auth.sign_in",
           subject=user["username"], actor=user["username"],
           detail={"ip": ip or None})

    return Principal(
        user_id=user["id"],
        username=user["username"],
        full_name=user["full_name"],
        tenant_id=user["tenant_id"],
        site_roles=_roles_of(conn, user["id"]),
        must_change_password=user["must_change_password"],
    ), token


def _fail(conn, throttle, ip, username, reason, user_id=None, detail=None):
    """
    Record the attempt, count it, COMMIT, and raise the one message.

    THE COMMIT IS THE WHOLE POINT, and leaving it out was a bug this
    module shipped with until it was run against a real install.

    Everything else in this application leaves the transaction to the
    caller's `with queries.connect(...)` block. That block sees an
    exception and ROLLS BACK - so the audit row written here and the
    `failed_logins` increment written just above were both being
    discarded by the very act of refusing the sign-in.

    What it cost, measured on the live dashboard: eight wrong passwords
    in a row left `failed_logins` at 0 and the audit table with no
    record of any of them. So §11's requirement to detect and report
    attempts at unauthorised use was not met, and the per-account
    lockout could never trigger no matter how long somebody guessed.

    A refusal is therefore its own transaction. The SUCCESS path still
    leaves committing to the caller, because a session row and its audit
    entry have to land together or not at all.
    """
    if throttle is not None:
        throttle.fail(ip)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tenant ORDER BY id LIMIT 1")
            tenant = cur.fetchone()
        if tenant is not None:
            record(conn, tenant_id=tenant["id"], action="auth.failed",
                   subject=username or "(blank)", actor=None,
                   detail={"reason": reason, "ip": ip or None,
                           **(detail or {})})
        conn.commit()
    except Exception:                                   # noqa: BLE001
        # An audit write that fails must not turn a refused sign-in into
        # a 500 - that would be a way to tell, from outside, that
        # something about this particular account is different. The
        # refusal still happens; only the record of it is lost, and that
        # is worth a line in the log.
        log.warning("could not record a failed sign-in for %r", username)
    raise SignInRejected(SAME_ANSWER, reason)


# ---------------------------------------------------------------------------
# staying signed in
# ---------------------------------------------------------------------------

def resolve(conn: psycopg.Connection, token: str | None, *,
            slide: bool = True) -> Principal | None:
    """
    The signed-in person for this token, or None.

    `slide` is the whole automatic-logoff mechanism. True for a real
    interaction - a page, a form, a chart somebody asked for. FALSE for a
    background poll, because this dashboard refreshes four panels every
    five seconds and a session that treated its own traffic as presence
    would never expire. See web/app.py, which decides which is which.
    """
    if not token:
        return None
    digest = security.token_hash(token)
    with conn.cursor() as cur:
        cur.execute(RESOLVE_SESSION, {"token_hash": digest})
        row = cur.fetchone()
    if row is None:
        return None
    if row["disabled_at"] is not None:
        # Disabled while signed in. End it now rather than waiting for
        # the idle timer - a person whose access was just removed should
        # not keep a working session for another fifteen minutes.
        end(conn, token, "account disabled")
        return None

    if slide:
        with conn.cursor() as cur:
            cur.execute(SLIDE_SESSION, {"token_hash": digest,
                                        "expires_at": _expiry()})

    return Principal(
        user_id=row["user_id"],
        username=row["username"],
        full_name=row["full_name"],
        tenant_id=row["tenant_id"],
        site_roles=_roles_of(conn, row["user_id"]),
        must_change_password=row["must_change_password"],
        expires_at=row["expires_at"],
    )


def end(conn: psycopg.Connection, token: str | None,
        reason: str = "signed out") -> None:
    if not token:
        return
    with conn.cursor() as cur:
        cur.execute(END_SESSION, {"token_hash": security.token_hash(token),
                                  "reason": reason})


def end_all_for(conn: psycopg.Connection, user_id: int,
                reason: str = "password changed") -> int:
    """
    End every session a user holds.

    Called on a password change and on a disable. A password changed
    because it may have been learned is worth nothing if the session
    opened with the old one keeps working.
    """
    with conn.cursor() as cur:
        cur.execute(END_USER_SESSIONS, {"user_id": user_id, "reason": reason})
        return cur.rowcount


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

INSERT_USER = """
INSERT INTO app_user (tenant_id, username, full_name, password_hash,
                      password_changed_at, must_change_password, created_by)
VALUES (%(tenant_id)s, %(username)s, %(full_name)s, %(password_hash)s,
        now(), %(must_change)s, %(created_by)s)
RETURNING id, username, full_name, created_at
"""

GRANT_ROLE = """
INSERT INTO user_role (user_id, site_id, role, granted_by, expires_at)
VALUES (%(user_id)s, %(site_id)s, %(role)s, %(granted_by)s, %(expires_at)s)
ON CONFLICT (user_id, site_id, role) DO UPDATE
    SET granted_by = EXCLUDED.granted_by, expires_at = EXCLUDED.expires_at
"""

USERNAME = __import__("re").compile(r"[a-z0-9][a-z0-9._-]{1,30}")


def create_user(conn: psycopg.Connection, *, username: str, full_name: str,
                password: str, roles, site_id: int | None = None,
                must_change: bool = False,
                actor: str | None = None) -> dict:
    """
    Create an account and grant it roles at a site. Does not commit.

    `must_change` is set for a password an administrator chose, so the
    holder replaces it before doing anything - otherwise an administrator
    knows a working password for somebody else's account, and attribution
    is gone.
    """
    username = (username or "").strip()
    full_name = (full_name or "").strip()

    if not USERNAME.fullmatch(username):
        raise WriteRejected(
            "A username is 2 to 31 characters: lowercase letters, digits, "
            "dot, dash or underscore. It appears in the audit log beside "
            "every change the person makes.", "username")
    if not full_name:
        raise WriteRejected(
            "Give the person's full name. The audit log shows the "
            "username; a reader in five years needs the name.", "full_name")

    roles = tuple(roles)
    if not roles:
        raise WriteRejected(
            "Give the account at least one role, or it can sign in and do "
            "nothing at all.", "roles")
    unknown = [r for r in roles if r not in perms.ROLES]
    if unknown:
        raise WriteRejected(f"{unknown} are not roles.", "roles")

    security.check_password(password, username, full_name)

    from .writes import one_site
    site = one_site(conn)
    site_id = site["id"] if site_id is None else site_id

    with conn.cursor() as cur:
        cur.execute(FIND_USER, {"username": username})
        if cur.fetchone() is not None:
            raise WriteRejected(
                f"There is already an account called {username!r}.",
                "username")

        cur.execute(INSERT_USER, {
            "tenant_id": site["tenant_id"], "username": username,
            "full_name": full_name,
            "password_hash": security.hash_password(password),
            "must_change": must_change, "created_by": actor,
        })
        user = cur.fetchone()

        for role in roles:
            cur.execute(GRANT_ROLE, {
                "user_id": user["id"], "site_id": site_id, "role": role,
                "granted_by": actor,
                # §10 time-boxes the support role. Nothing else expires.
                "expires_at": None,
            })

    record(conn, tenant_id=site["tenant_id"], action="user.add",
           subject=username, actor=actor,
           detail={"full_name": full_name, "roles": list(roles),
                   "site_id": site_id, "must_change_password": must_change})
    return dict(user)


def set_password(conn: psycopg.Connection, *, user_id: int, username: str,
                 full_name: str, password: str, actor: str | None = None,
                 must_change: bool = False) -> None:
    """
    Replace a password and end every session the account holds.

    Ending the sessions is the point. A password changed because it may
    have been learned is worth nothing while the session opened with the
    old one keeps working.
    """
    security.check_password(password, username, full_name)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_user "
            "   SET password_hash = %(hash)s, password_changed_at = now(),"
            "       must_change_password = %(must_change)s,"
            "       failed_logins = 0, locked_until = NULL "
            " WHERE id = %(id)s RETURNING tenant_id",
            {"hash": security.hash_password(password), "id": user_id,
             "must_change": must_change})
        row = cur.fetchone()
    ended = end_all_for(conn, user_id, "password changed")
    record(conn, tenant_id=row["tenant_id"], action="user.password",
           subject=username, actor=actor,
           detail={"by_themselves": actor == username,
                   "sessions_ended": ended})


def set_disabled(conn: psycopg.Connection, *, user_id: int, username: str,
                 disabled: bool, reason: str = "",
                 actor: str | None = None) -> None:
    """Disable or re-enable an account. Never deletes one."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_user "
            "   SET disabled_at = CASE WHEN %(off)s THEN now() ELSE NULL END,"
            "       disabled_by = CASE WHEN %(off)s THEN %(actor)s ELSE NULL END,"
            "       disabled_reason = CASE WHEN %(off)s THEN %(reason)s ELSE NULL END,"
            "       failed_logins = 0, locked_until = NULL "
            " WHERE id = %(id)s RETURNING tenant_id",
            {"off": disabled, "actor": actor, "reason": reason or None,
             "id": user_id})
        row = cur.fetchone()
    ended = end_all_for(conn, user_id, "account disabled") if disabled else 0
    record(conn, tenant_id=row["tenant_id"],
           action="user.disable" if disabled else "user.enable",
           subject=username, actor=actor,
           detail={"reason": reason or None, "sessions_ended": ended})


LIST_USERS = """
SELECT u.id, u.username, u.full_name, u.created_at, u.created_by,
       u.last_login_at, u.disabled_at, u.disabled_reason,
       u.must_change_password, u.locked_until,
       coalesce(array_agg(r.role ORDER BY r.role)
                FILTER (WHERE r.role IS NOT NULL), '{}') AS roles,
       (SELECT count(*) FROM session s
         WHERE s.user_id = u.id AND s.ended_at IS NULL
           AND s.expires_at > now())                     AS live_sessions
FROM   app_user u
LEFT   JOIN user_role r ON r.user_id = u.id
                       AND (r.expires_at IS NULL OR r.expires_at > now())
GROUP  BY u.id
ORDER  BY u.disabled_at NULLS FIRST, lower(u.username)
"""


def list_users(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(LIST_USERS)
        return cur.fetchall()


def user_by_name(conn: psycopg.Connection, username: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(FIND_USER, {"username": username})
        return cur.fetchone()
