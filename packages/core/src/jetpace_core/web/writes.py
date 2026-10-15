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

from .. import devices as devices_


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
    low = _as_number(low, "low set point", "low")
    high = _as_number(high, "high set point", "high")

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

def _as_number(value: Any, what: str, field: str = "") -> float | None:
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
        raise WriteRejected(f"{what.capitalize()} must be a number.",
                            field) from None


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, str):
        value = value.strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        raise WriteRejected(
            f"{what.capitalize()} must be a whole number.") from None


# ---------------------------------------------------------------------------
# commissioning a device
#
# Seven kinds of row in one transaction: the gateway if the converter is
# new, the device model, the parameters the type reads, the device, one
# device_parameter per point, the limit, and the audit row. Nothing here
# commits, so the caller's connection context manager makes it all or
# nothing - and all or nothing is the only acceptable outcome. A device row
# without its device_parameter rows is polled and stores nothing; one
# without its limit has no specification; one without the audit row is a
# configuration change at a validated site that nobody can account for.
#
# It is also, deliberately, the same set of statements seed_prototype.py
# runs from the TOML file. The two paths have to produce identical rows or
# the dashboard and the gateway sheet describe different installs.
# ---------------------------------------------------------------------------

SITES = "SELECT id, tenant_id, name FROM site ORDER BY id"

CODE_TAKEN = """
SELECT d.code, d.name, d.retired_at IS NOT NULL AS retired
FROM   device d
WHERE  d.tenant_id = %(tenant_id)s AND d.code = %(code)s
"""

UNIT_TAKEN = """
SELECT d.code, d.name
FROM   device d
JOIN   gateway g ON g.id = d.gateway_id
WHERE  g.host = %(host)s AND g.port = %(port)s
  AND  d.unit_id = %(unit_id)s
  AND  d.retired_at IS NULL
"""

CODES_LIKE = """
SELECT d.code
FROM   device d
WHERE  d.tenant_id = %(tenant_id)s AND d.code LIKE %(pattern)s
"""

GATEWAY_UPSERT = """
INSERT INTO gateway (tenant_id, site_id, label, host, port,
                     timeout_ms, gap_ms, scan_interval_ms,
                     offline_backoff_cycles)
VALUES (%(tenant_id)s, %(site_id)s, %(label)s, %(host)s, %(port)s,
        %(timeout_ms)s, %(gap_ms)s, %(scan_interval_ms)s, %(backoff)s)
ON CONFLICT (tenant_id, host, port) DO UPDATE
    SET host = EXCLUDED.host
RETURNING id, label, (xmax = 0) AS created
"""

MODEL_UPSERT = """
INSERT INTO device_model (code, revision, vendor, kind, datasheet_ref)
VALUES (%(code)s, %(revision)s, %(vendor)s, %(kind)s, %(datasheet_ref)s)
ON CONFLICT (code, revision) DO UPDATE SET vendor = EXCLUDED.vendor
RETURNING id
"""

PARAMETER_UPSERT = """
INSERT INTO parameter (key, label, unit, aggregation, decimals)
VALUES (%(key)s, %(label)s, %(unit)s, %(aggregation)s, %(decimals)s)
ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label
RETURNING id
"""

DEVICE_INSERT = """
INSERT INTO device (tenant_id, site_id, gateway_id, model_id, code, name,
                    unit_id, scan_class, commissioned_at)
VALUES (%(tenant_id)s, %(site_id)s, %(gateway_id)s, %(model_id)s, %(code)s,
        %(name)s, %(unit_id)s, %(scan_class)s, now())
-- The whole row that mattered, not just its id. A caller rendering a
-- confirmation screen should read what the DATABASE stored rather than
-- echo back what it sent: `commissioned_at` comes from the server's clock
-- and `unit_id` has been through an integer column, so the two can differ
-- from the form's strings in ways worth seeing.
RETURNING id, code, name, unit_id, scan_class, commissioned_at
"""

DEVICE_PARAMETER_INSERT = """
INSERT INTO device_parameter (device_id, param, scale)
VALUES (%(device_id)s, %(param)s, %(scale)s)
ON CONFLICT (device_id, param) DO UPDATE
    SET enabled = true, scale = EXCLUDED.scale
"""

# What a converter gets the first time it is seen. These are GatewaySpec's
# defaults and 0006's column defaults, written out here rather than left to
# the database so a reader of this file can see what a new bus is given.
#
# An EXISTING converter keeps the label and timings it was commissioned
# with - that is why GATEWAY_UPSERT's DO UPDATE touches only `host`.
# Adding a fourth device to a bus is not an occasion to re-tune the bus,
# and quietly applying a form's defaults would change the scan rate of the
# three devices already on it.
NEW_GATEWAY_TIMEOUT_MS = 2000
NEW_GATEWAY_GAP_MS = 25
NEW_GATEWAY_INTERVAL_MS = 1000
NEW_GATEWAY_BACKOFF = 5

# Modbus: 0 is a broadcast and 248 upwards is reserved.
MAX_UNIT_ID = 247


def one_site(conn: psycopg.Connection) -> dict:
    """
    The site a commissioned device belongs to.

    An install serves one customer at one site and there is no site
    selector anywhere in the UI, so this picks the only one. If there is
    ever more than one it REFUSES rather than taking the first: a device
    filed against the wrong site is missing from the right one's reports,
    and nothing on the screen would have asked.
    """
    with conn.cursor() as cur:
        cur.execute(SITES)
        sites = cur.fetchall()
    if not sites:
        raise WriteRejected(
            "This database has no site yet, so there is nowhere to "
            "commission a device into. Run the provisioning step first.")
    if len(sites) > 1:
        names = ", ".join(s["name"] for s in sites)
        raise WriteRejected(
            f"This install has {len(sites)} sites ({names}), and the form "
            f"has no way to say which one. Commission from configuration "
            f"until it does.")
    return sites[0]


def suggest_code(conn: psycopg.Connection, tenant_id: int,
                 prefix: str) -> str:
    """
    The lowest free `PREFIX-NNN`, as a suggestion for the form.

    LOWEST FREE rather than highest-plus-one, so retiring DP-002 and
    commissioning its replacement in the same room offers DP-002 again
    instead of DP-004.

    Retired codes count as taken. A code is stable forever and readings
    are stored against the device id, so reusing one would put a new
    instrument's readings on a page that also shows the old one's
    history, with nothing to separate them.
    """
    prefix = prefix or "DEV"
    with conn.cursor() as cur:
        cur.execute(CODES_LIKE, {"tenant_id": tenant_id,
                                 "pattern": f"{prefix}-%"})
        taken = {r["code"] for r in cur.fetchall()}
    for n in range(1, 1000):
        candidate = f"{prefix}-{n:03d}"
        if candidate not in taken:
            return candidate
    return f"{prefix}-{len(taken) + 1}"


def commission_device(
        conn: psycopg.Connection, *,
        device_type: Any,
        code: str,
        name: str,
        host: str,
        port: Any,
        unit_id: Any,
        low: Any = None,
        high: Any = None,
        gateway_label: str = "",
        answered: bool = True,
        detail: str = "",
        reason: str = "",
        actor: str | None = None) -> dict:
    """
    Create a device, its parameters and its limits, and record all of it.

    `device_type` is a `jetpace_core.devices.DeviceType`: the register map
    plus the product's decisions about it. Which registers to read, what
    they mean, and how often to scan come from there and nowhere else.
    What the operator supplies is where the instrument is and what it is
    called.

    `answered` is whether the probe found it. A device can legitimately be
    commissioned before it answers - installed and not yet powered, or
    cabled next week - so this does not refuse, but it requires a `reason`
    and records both. "It did not answer and somebody said go ahead
    anyway, because X" is a fact worth keeping.

    Returns the device row. Does not commit.
    """
    code = (code or "").strip()
    name = (name or "").strip()
    host = (host or "").strip()
    gateway_label = (gateway_label or "").strip()
    reason = (reason or "").strip()

    # NORMALISE THE SET POINTS BEFORE THE "did they give any" TEST.
    #
    # An HTML form posts "" for a field nobody filled in, and "" is not
    # None - so the test below for whether to create a limit at all saw
    # two empty strings, decided a limit was wanted, and `set_limit`
    # refused it as having neither side. The whole commission failed,
    # with a message about set points, for a device whose operator had
    # deliberately not given any.
    #
    # Converting here rather than inside the test also means a
    # mistyped figure is reported against ITS OWN field rather than as
    # "low", which is what set_limit would have said for either.
    low = _as_number(low, "low set point", "low")
    high = _as_number(high, "high set point", "high")

    if not devices_.CODE.fullmatch(code):
        raise WriteRejected(
            "A device code is letters, digits, hyphens and underscores, up "
            "to 31 characters. It appears in a web address, an MQTT topic "
            "and a CSV column, and it can never be changed afterwards.",
            "code")
    if not name:
        raise WriteRejected(
            "Give the device a name. It is what an operator reads on every "
            "screen and in every report.", "name")
    if not host:
        raise WriteRejected("Where is the converter? Give its address.",
                            "host")
    if " " in host:
        raise WriteRejected(f"{host!r} is not an address.", "host")

    port = _as_int(port, "port")
    unit_id = _as_int(unit_id, "slave id")
    if not 1 <= port <= 65535:
        raise WriteRejected(f"{port} is not a port number.", "port")
    if not 1 <= unit_id <= MAX_UNIT_ID:
        raise WriteRejected(
            f"A Modbus slave address is 1 to {MAX_UNIT_ID}. 0 is a "
            f"broadcast, and {MAX_UNIT_ID + 1} upwards is reserved.",
            "unit_id")
    if not answered and not reason:
        raise WriteRejected(
            "The device did not answer. Say why it should be commissioned "
            "anyway - it goes into the audit record.", "reason")

    site = one_site(conn)
    tenant_id = site["tenant_id"]
    model = device_type.model

    with conn.cursor() as cur:
        # Both of these are constraints in the schema too, which is the
        # actual guarantee. They are checked here so the operator reads
        # WHICH device is in the way rather than a constraint name.
        cur.execute(CODE_TAKEN, {"tenant_id": tenant_id, "code": code})
        clash = cur.fetchone()
        if clash is not None:
            if clash["retired"]:
                raise WriteRejected(
                    f"{code} belonged to {clash['name']!r}, which has been "
                    f"retired. Its readings are still stored, so the code "
                    f"cannot be reused - pick another.", "code")
            raise WriteRejected(
                f"{code} is already {clash['name']!r}.", "code")

        cur.execute(UNIT_TAKEN, {"host": host, "port": port,
                                 "unit_id": unit_id})
        occupied = cur.fetchone()
        if occupied is not None:
            raise WriteRejected(
                f"Slave address {unit_id} on {host}:{port} is already "
                f"{occupied['code']} ({occupied['name']}). Two devices "
                f"answering to one address collide and neither reads "
                f"reliably.", "unit_id")

        cur.execute(GATEWAY_UPSERT, {
            "tenant_id": tenant_id, "site_id": site["id"],
            "label": gateway_label or f"{host}:{port}",
            "host": host, "port": port,
            "timeout_ms": NEW_GATEWAY_TIMEOUT_MS,
            "gap_ms": NEW_GATEWAY_GAP_MS,
            "scan_interval_ms": NEW_GATEWAY_INTERVAL_MS,
            "backoff": NEW_GATEWAY_BACKOFF,
        })
        gateway = cur.fetchone()

        cur.execute(MODEL_UPSERT, {
            "code": model.code, "revision": model.revision,
            "vendor": model.vendor, "kind": device_type.kind,
            "datasheet_ref": model.datasheet_ref,
        })
        model_id = cur.fetchone()["id"]

        param_ids: dict[str, int] = {}
        for point in device_type.points:
            cur.execute(PARAMETER_UPSERT, {
                "key": point.param, "label": point.label,
                "unit": point.unit, "aggregation": point.aggregation,
                "decimals": point.decimals,
            })
            param_ids[point.param] = cur.fetchone()["id"]

        cur.execute(DEVICE_INSERT, {
            "tenant_id": tenant_id, "site_id": site["id"],
            "gateway_id": gateway["id"], "model_id": model_id,
            "code": code, "name": name, "unit_id": unit_id,
            "scan_class": device_type.scan_class,
        })
        device = cur.fetchone()

        for point in device_type.points:
            cur.execute(DEVICE_PARAMETER_INSERT, {
                "device_id": device["id"],
                "param": param_ids[point.param],
                # NULL, not the catalogue's figure. Null means "whatever
                # the product says", so a corrected scale factor shipped
                # in a release reaches devices commissioned before it.
                # Writing the number here would freeze every device at the
                # figure that happened to be current on its own
                # commissioning day - and debt #6 says this figure is
                # going to be corrected.
                "scale": None,
            })

    record(conn, tenant_id=tenant_id, action="device.add", subject=code,
           actor=actor,
           detail={
               "name": name,
               "model": model.code,
               "model_revision": model.revision,
               "gateway": {"host": host, "port": port,
                           "label": gateway["label"],
                           "created": bool(gateway["created"])},
               "unit_id": unit_id,
               "scan_class": device_type.scan_class,
               "parameters": [p.param for p in device_type.points],
               # The commissioning evidence: what the instrument was
               # reading when it was accepted onto the system. It is the
               # one measurement nobody can reconstruct afterwards.
               "probe": {"answered": answered, "detail": detail},
               "reason": reason or None,
           })

    if low is not None or high is not None:
        if device_type.limits is None:
            raise WriteRejected(
                f"{device_type.code} has no measured parameter to put a set "
                f"point on.", "low")
        set_limit(conn, device_code=code, param=device_type.limits,
                  low=low, high=high, set_by=actor,
                  reason=reason or "commissioned")

    return dict(device) | {
        "code": code, "name": name,
        "gateway_label": gateway["label"],
        "gateway_created": bool(gateway["created"]),
    }
