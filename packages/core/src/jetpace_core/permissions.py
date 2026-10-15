"""
What each role may do, as one table.

This is specification §10's role matrix and nothing else. It is a module
of its own, with no imports of ours, so that the answer to "who can
change a set point" is one file a reviewer or an auditor can read end to
end - rather than a condition scattered across however many route
handlers happen to need it.

HOW TO READ IT. A ROLE is what somebody is. A PERMISSION is one thing the
software can do. `GRANTS` maps the first to the second and is the whole
policy; everything else in this file is lookups over it.

A PERMISSION THAT GUARDS NOTHING IS STILL DECLARED. `alarm.acknowledge`
is here and there are no alarms yet; `tariff.edit` is here and there are
no tariffs. They come from the specification's table, and declaring the
vocabulary now means the feature arrives with its permission already
decided rather than invented by whoever writes the route. A test asserts
every permission is in the matrix, and a separate one lists which have no
caller yet, so the gap is visible instead of forgotten.

WHAT THIS FILE DOES NOT DO. It does not know about sites. A role is
granted per site (§10, and migrations/0008), so the question is always
"may this user do X at site Y" - which is `web/sessions.py`'s job,
because it needs the database to answer it. Keeping scope out of here
keeps the policy readable.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------

# Exactly the seven in §10, in the order the specification lists them,
# which is also roughly increasing authority - but NOT exactly, and the
# code must never assume it is. `qa` and `support` are sideways: QA may
# sign records and change no configuration at all, and support may read
# diagnostics and, by default, no instrument data. A numeric "level"
# would have to put them somewhere on a line they are not on.
ROLES: tuple[str, ...] = (
    "viewer", "operator", "manager", "engineer", "administrator",
    "qa", "support",
)

ROLE_LABELS = {
    "viewer": "Viewer",
    "operator": "Operator",
    "manager": "Manager",
    "engineer": "Engineer",
    "administrator": "Administrator",
    "qa": "QA",
    "support": "Jetpace support",
}

ROLE_NOTES = {
    "viewer": "Dashboards, trends and reports. Changes nothing.",
    "operator": "A viewer who can acknowledge and shelve alarms.",
    "manager": "An operator who can export, and set limits and targets.",
    "engineer": "A manager who can commission devices and register maps.",
    "administrator": "Everything, including users. Cannot alter the audit log.",
    "qa": "Reviews and signs records. Changes no configuration.",
    "support": "Diagnostics and logs, time-boxed. No instrument data by default.",
}


@dataclass(frozen=True)
class Permission:
    key: str
    what: str            # what it lets somebody do, in a few words


def _p(key: str, what: str) -> str:
    PERMISSIONS[key] = Permission(key, what)
    return key


PERMISSIONS: dict[str, Permission] = {}

# --- reading ---------------------------------------------------------------
VIEW = _p("view", "Open the dashboards, the device pages and the trends")
REPORT_RUN = _p("report.run", "Run a report on screen")
EXPORT = _p("export", "Download readings or the device list as a file")

# --- alarms (§10 step 6; nothing raises one yet) ---------------------------
ALARM_ACK = _p("alarm.acknowledge", "Acknowledge an alarm")
ALARM_SHELVE = _p("alarm.shelve", "Shelve an alarm")

# --- configuration ---------------------------------------------------------
LIMIT_SET = _p("limit.set", "Set a low or high set point on a device")
DEVICE_ADD = _p("device.add", "Commission a device")
DEVICE_EDIT = _p("device.edit", "Change a device, its register map or scan class")
DEVICE_RETIRE = _p("device.retire", "Retire a device")
TARIFF_EDIT = _p("tariff.edit", "Change tariffs, slabs and time-of-use")
TARGET_EDIT = _p("target.edit", "Change targets and baselines")

# --- records (environmental product; §11 subpart C) ------------------------
RECORD_SIGN = _p("record.sign", "Sign a record electronically")
RECORD_REVIEW = _p("record.review", "Mark a record or an alarm period reviewed")

# --- the system ------------------------------------------------------------
USER_MANAGE = _p("user.manage", "Create, disable and re-role users")
AUDIT_READ = _p("audit.read", "Read the audit log")
DIAGNOSTICS = _p("diagnostics", "Read versions, logs and health")
UPDATE_APPLY = _p("update.apply", "Approve and apply a software update")

# NOTE there is deliberately no `audit.write` or `audit.delete`. §10:
# an administrator can do everything EXCEPT alter or delete the audit log.
# That is not enforced by withholding a permission - it is enforced by the
# database, which revokes UPDATE and DELETE on `audit` from the
# application role entirely (0005). A permission would imply the
# application could do it for somebody sufficiently privileged. It cannot.


# ---------------------------------------------------------------------------
# the matrix
#
# Written out per role rather than by inheritance. "Operator = viewer plus
# two things" is how the specification DESCRIBES it, and it is a bad way
# to STORE it: a permission added to viewer silently appears for all five
# roles above it, which is exactly the class of change nobody reviews.
# Seven explicit lists are longer and they are readable in one pass, which
# is the property that matters for a file an auditor will ask about.
# ---------------------------------------------------------------------------

GRANTS: dict[str, frozenset[str]] = {
    "viewer": frozenset({
        VIEW, REPORT_RUN,
    }),
    "operator": frozenset({
        VIEW, REPORT_RUN,
        ALARM_ACK, ALARM_SHELVE,
    }),
    "manager": frozenset({
        VIEW, REPORT_RUN, EXPORT,
        ALARM_ACK, ALARM_SHELVE,
        LIMIT_SET, TARGET_EDIT,
    }),
    "engineer": frozenset({
        VIEW, REPORT_RUN, EXPORT,
        ALARM_ACK, ALARM_SHELVE,
        LIMIT_SET, TARGET_EDIT,
        DEVICE_ADD, DEVICE_EDIT, DEVICE_RETIRE,
        DIAGNOSTICS,
    }),
    "administrator": frozenset({
        VIEW, REPORT_RUN, EXPORT,
        ALARM_ACK, ALARM_SHELVE,
        LIMIT_SET, TARGET_EDIT,
        DEVICE_ADD, DEVICE_EDIT, DEVICE_RETIRE,
        TARIFF_EDIT,
        USER_MANAGE, AUDIT_READ, DIAGNOSTICS, UPDATE_APPLY,
    }),
    # Sideways, not below. QA reads everything and signs records, and
    # changes NO configuration - that separation is the point of the role,
    # because a person who can both set a limit and sign off that it was
    # met is not an independent reviewer.
    "qa": frozenset({
        VIEW, REPORT_RUN, EXPORT,
        RECORD_SIGN, RECORD_REVIEW,
        AUDIT_READ,
    }),
    # Also sideways, and the narrowest. §10: "Diagnostics, versions, logs
    # - time-boxed, consented" and "cannot read instrument data by
    # default". So no VIEW: support sees whether the system is working,
    # not what it measured. The time-boxing is `user_role.expires_at`.
    "support": frozenset({
        DIAGNOSTICS,
    }),
}


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------

def allowed(roles: frozenset[str] | set[str] | tuple[str, ...],
            permission: str) -> bool:
    """
    Whether any of these roles carries this permission.

    ANY, because a user can hold several roles at one site - an engineer
    who is also the administrator is one person with two grants, and
    taking the union is the only reading that is not surprising.

    An unknown role contributes nothing rather than raising. The database
    constrains the column, so an unknown value here means somebody has
    been in the table by hand, and the safe reading of a role this
    software does not understand is that it grants nothing.
    """
    if permission not in PERMISSIONS:
        raise KeyError(
            f"{permission!r} is not a declared permission. Add it to "
            f"permissions.py with the role that carries it, rather than "
            f"checking a string the matrix has never heard of.")
    return any(permission in GRANTS.get(role, frozenset()) for role in roles)


def permissions_of(roles) -> frozenset[str]:
    """Everything these roles can do between them."""
    out: set[str] = set()
    for role in roles:
        out |= GRANTS.get(role, frozenset())
    return frozenset(out)


def roles_with(permission: str) -> tuple[str, ...]:
    """Which roles carry this, in ROLES order. For the users screen."""
    return tuple(r for r in ROLES if permission in GRANTS.get(r, frozenset()))
