"""
The statements that change something.

Separate from `queries.py` deliberately, and not because the file was
getting long. Everything in there answers a question; everything in here
alters the configuration of an instrument somebody will later cite in a
batch record. Keeping them apart means a reviewer reading a diff can tell
which kind of change they are looking at from the filename, and it means
"does the dashboard write?" is answered by whether this module is
imported rather than by reading six hundred lines of SQL.

THREE RULES, all of them enforced below rather than remembered.

  EVERY WRITE RECORDS ITSELF. An audit row goes in alongside, in the same
  transaction, from inside the same function. Not as a courtesy from the
  caller - a caller can forget, and the one time it is forgotten is the
  one time somebody asks.

  NOTHING HERE COMMITS. Each function takes a connection and leaves it
  dirty; the caller's `with queries.connect(dsn) as conn:` commits on a
  clean exit and rolls back on an exception. That is what makes "the
  device, its parameters, its limits and the audit row, or none of them"
  true without a single explicit transaction statement.

  A REJECTION IS A SENTENCE, NOT A TRACEBACK. The database has CHECK
  constraints for the same conditions and they are the real guarantee;
  these checks exist so the operator reads "the low set point must be
  below the high one" instead of
  `CheckViolation: device_limit_ordered`.

NOTHING HERE WRITES TO AN INSTRUMENT. Modbus writes are disabled at build
time, not by configuration (CLAUDE.md), and commissioning a device is a
database act: it records what was found on the bus, it does not configure
the device. The register that sets a slave address is in
`NEVER_WRITE` and getting it wrong costs a site visit.
"""

from __future__ import annotations

from typing import Any

import psycopg


class WriteRejected(Exception):
    """
    A write refused for a reason the operator can act on.

    Carries a sentence meant to be rendered next to the field that caused
    it. Anything that is not this is a fault, and should reach the logs
    rather than the screen.
    """

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

AUDIT_INSERT = """
INSERT INTO audit (tenant_id, actor, action, subject, detail)
VALUES (%(tenant_id)s, %(actor)s, %(action)s, %(subject)s, %(detail)s)
RETURNING id, at
"""


def record(conn: psycopg.Connection, *, tenant_id: int, action: str,
           subject: str, detail: dict[str, Any] | None = None,
           actor: str | None = None) -> dict:
    """
    Append one audit row.

    `actor` is None on every install that has no user model, which is all
    of them today (architecture.md §10). It is a parameter rather than a
    hardcoded null so that the first login needs no change here, and it is
    never defaulted to a string like "dashboard" - a name in that column
    has to mean a person who could be asked about it.

    `detail` is stored as submitted, including values that were rejected
    further down. An audit log that only records what succeeded cannot
    answer "did somebody try".
    """
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(AUDIT_INSERT, {
            "tenant_id": tenant_id,
            "actor": actor,
            "action": action,
            "subject": subject,
            "detail": Jsonb(detail or {}),
        })
        return cur.fetchone()


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------

# The device and the parameter are resolved here rather than taken as ids,
# because every caller has a code and a key - that is what the operator
# typed and what the poller speaks - and because resolving them is also
# the existence check. A limit against a device code that is not there is
# a rejection with a readable message, not a foreign key violation.
RESOLVE_DEVICE = """
SELECT d.id, d.tenant_id, d.retired_at
FROM   device d
WHERE  d.code = %(code)s
"""

RESOLVE_PARAM = "SELECT id FROM parameter WHERE key = %(key)s"

LIMIT_INSERT = """
INSERT INTO device_limit (tenant_id, device_id, param, low, high,
                          set_by, reason)
VALUES (%(tenant_id)s, %(device_id)s, %(param)s, %(low)s, %(high)s,
        %(set_by)s, %(reason)s)
RETURNING id, set_at
"""

# What the limit was before, so the audit row can say what changed rather
# than only what it became. One row off the front of device_limit_current.
LIMIT_PREVIOUS = """
SELECT low, high
FROM   device_limit
WHERE  device_id = %(device_id)s AND param = %(param)s
ORDER  BY set_at DESC
LIMIT  1
"""


def set_limit(conn: psycopg.Connection, *, device_code: str, param: str,
              low: float | None, high: float | None,
              reason: str | None = None,
              set_by: str | None = None) -> dict:
    """
    Put a low and high set point on one parameter of one device.

    An INSERT, always - never an update, because the application role has
    no UPDATE on the table and because last month's readings have to stay
    judgeable against the limit that applied last month. The limit in
    force is the most recent row; `queries.device_limits` reads it with
    DISTINCT ON.

    Returns the new row's id and timestamp. Does not commit.
    """
    low = _as_number(low, "low set point")
    high = _as_number(high, "high set point")

    if low is None and high is None:
        raise WriteRejected(
            "A limit needs at least a low or a high set point.", "low")
    if low is not None and high is not None and low >= high:
        raise WriteRejected(
            f"The low set point ({low:g}) must be below the high one "
            f"({high:g}).", "low")

    with conn.cursor() as cur:
        cur.execute(RESOLVE_DEVICE, {"code": device_code})
        device = cur.fetchone()
        if device is None:
            raise WriteRejected(f"There is no device {device_code!r}.",
                                "device")
        cur.execute(RESOLVE_PARAM, {"key": param})
        parameter = cur.fetchone()
        if parameter is None:
            raise WriteRejected(
                f"There is no parameter {param!r} to put a limit on.",
                "param")

        cur.execute(LIMIT_PREVIOUS, {"device_id": device["id"],
                                     "param": parameter["id"]})
        before = cur.fetchone()

        cur.execute(LIMIT_INSERT, {
            "tenant_id": device["tenant_id"],
            "device_id": device["id"],
            "param": parameter["id"],
            "low": low, "high": high,
            "set_by": set_by, "reason": reason,
        })
        row = cur.fetchone()

    record(conn, tenant_id=device["tenant_id"], action="limit.set",
           subject=device_code, actor=set_by,
           detail={
               "parameter": param,
               "low": low, "high": high,
               # Null on the first limit a device is given, which is how
               # "commissioned with these set points" is told apart from
               # "somebody changed them afterwards".
               "previous_low": _float_or_none(before and before["low"]),
               "previous_high": _float_or_none(before and before["high"]),
               "reason": reason,
           })
    return row


# ---------------------------------------------------------------------------

def _as_number(value: Any, what: str) -> float | None:
    """
    An empty field means "no limit on this side"; anything unparseable is
    a rejection naming the field.

    A blank string has to become None rather than 0, and that conversion
    is the whole reason this exists: an HTML form posts "" for a field
    nobody filled in, and float("") raising is the good case. The bad case
    would be a helper that treated it as zero, which on a pressure cascade
    is a real and plausible set point and so would not look wrong
    anywhere.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise WriteRejected(f"{what.capitalize()} must be a number.") from None


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)
