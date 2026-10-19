"""
The role matrix, checked against the specification that defines it.

Specification §10 gives a seven-row table of what each role can and
cannot do. These tests are that table, read back. Where the specification
says "cannot", there is a test that it cannot — those are the ones worth
having, because a permission wrongly granted is invisible until somebody
uses it.

Pure: imports one module that imports nothing.
"""

from pathlib import Path

import pytest

from jetpace_core import permissions as P

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "migrations" / "0008_identity.sql"


def can(role: str, permission: str) -> bool:
    return P.allowed({role}, permission)


# --- the vocabulary matches the database ----------------------------------

def test_the_roles_are_exactly_the_seven_in_the_specification():
    assert set(P.ROLES) == {
        "viewer", "operator", "manager", "engineer", "administrator",
        "qa", "support",
    }


def test_the_roles_are_exactly_the_migrations_check_constraint():
    """
    A role accepted here and rejected by the database would be a grant
    that fails on INSERT, long after somebody chose it in a dropdown.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    block = sql.split("role       text        NOT NULL CHECK (role IN (")[1]
    block = block.split("))")[0]
    in_migration = {w.strip().strip("',\n ") for w in block.split(",")}
    assert in_migration - {""} == set(P.ROLES)


def test_every_role_has_a_label_and_a_note():
    """Both are shown on the users screen; a missing one renders blank."""
    for role in P.ROLES:
        assert P.ROLE_LABELS.get(role)
        assert P.ROLE_NOTES.get(role)


def test_every_granted_permission_is_a_declared_one():
    """
    A typo in a frozenset would be a permission nobody holds, silently -
    the role would simply never be able to do the thing.
    """
    for role, granted in P.GRANTS.items():
        unknown = granted - set(P.PERMISSIONS)
        assert not unknown, f"{role}: {unknown}"


def test_every_role_appears_in_the_matrix():
    assert set(P.GRANTS) == set(P.ROLES)


def test_checking_an_undeclared_permission_raises():
    """
    Loudly, and not False. A misspelled permission string that returned
    False would make a route deny everybody, which reads as a
    configuration problem rather than a typo.
    """
    with pytest.raises(KeyError):
        P.allowed({"administrator"}, "device.delete")


# --- what the specification says each role CAN do -------------------------

def test_viewer_sees_dashboards_and_runs_reports():
    assert can("viewer", P.VIEW)
    assert can("viewer", P.REPORT_RUN)


def test_operator_is_a_viewer_who_can_acknowledge():
    assert can("operator", P.VIEW)
    assert can("operator", P.ALARM_ACK)
    assert can("operator", P.ALARM_SHELVE)


def test_manager_adds_export_and_limits():
    assert can("manager", P.EXPORT)
    assert can("manager", P.LIMIT_SET)
    assert can("manager", P.TARGET_EDIT)


def test_engineer_adds_devices_and_register_maps():
    assert can("engineer", P.DEVICE_ADD)
    assert can("engineer", P.DEVICE_EDIT)
    assert can("engineer", P.DEVICE_RETIRE)


def test_administrator_adds_tariffs_and_users():
    assert can("administrator", P.TARIFF_EDIT)
    assert can("administrator", P.USER_MANAGE)


def test_qa_signs_and_reviews_records():
    assert can("qa", P.RECORD_SIGN)
    assert can("qa", P.RECORD_REVIEW)


# --- what the specification says each role CANNOT do ----------------------
#
# The half that matters. A permission wrongly granted is invisible until
# somebody uses it, and then it is in the audit log for ever.

def test_a_viewer_changes_nothing():
    """§10: "Export, acknowledge, change anything" — all three denied."""
    assert not can("viewer", P.EXPORT)
    assert not can("viewer", P.ALARM_ACK)
    for permission in (P.LIMIT_SET, P.DEVICE_ADD, P.DEVICE_EDIT,
                       P.TARIFF_EDIT, P.USER_MANAGE, P.TARGET_EDIT):
        assert not can("viewer", permission), permission


def test_an_operator_cannot_change_limits_or_tariffs():
    assert not can("operator", P.LIMIT_SET)
    assert not can("operator", P.TARIFF_EDIT)
    assert not can("operator", P.EXPORT), "§10 withholds export from operator"


def test_a_manager_cannot_touch_tariffs_devices_or_users():
    assert not can("manager", P.TARIFF_EDIT)
    assert not can("manager", P.DEVICE_ADD)
    assert not can("manager", P.DEVICE_EDIT)
    assert not can("manager", P.USER_MANAGE)


def test_an_engineer_cannot_touch_tariffs_or_users():
    assert not can("engineer", P.TARIFF_EDIT)
    assert not can("engineer", P.USER_MANAGE)


def test_qa_changes_no_configuration_at_all():
    """
    The separation is the point of the role: somebody who can both set a
    limit and sign that it was met is not an independent reviewer.
    """
    for permission in (P.LIMIT_SET, P.DEVICE_ADD, P.DEVICE_EDIT,
                       P.DEVICE_RETIRE, P.TARIFF_EDIT, P.TARGET_EDIT,
                       P.USER_MANAGE, P.UPDATE_APPLY):
        assert not can("qa", permission), permission


def test_support_cannot_read_instrument_data_by_default():
    """
    §10: "Diagnostics, versions, logs — time-boxed, consented" and
    "Cannot read instrument data by default". So no VIEW: support sees
    whether the system works, not what it measured.
    """
    assert can("support", P.DIAGNOSTICS)
    assert not can("support", P.VIEW)
    assert not can("support", P.EXPORT)
    assert not can("support", P.REPORT_RUN)


def test_support_is_time_boxed_by_the_schema():
    """The specification time-boxes this role; a column has to carry it."""
    assert "expires_at" in MIGRATION.read_text(encoding="utf-8")


def test_nobody_can_alter_the_audit_log():
    """
    §10 says an administrator can do everything EXCEPT alter or delete
    the audit log. That is not enforced by withholding a permission - it
    is enforced by the database revoking UPDATE and DELETE on `audit`
    from the application role (0005). A permission would imply the
    software could do it for somebody sufficiently privileged.
    """
    assert "audit.write" not in P.PERMISSIONS
    assert "audit.delete" not in P.PERMISSIONS
    setup = (ROOT / "scripts" / "setup_postgres.py").read_text(encoding="utf-8")
    assert "REVOKE UPDATE, DELETE ON audit" in setup


# --- holding more than one role -------------------------------------------

def test_two_roles_are_the_union_of_what_they_allow():
    """
    An engineer who is also the administrator is one person with two
    grants. Any other reading is surprising.
    """
    both = {"engineer", "administrator"}
    assert P.allowed(both, P.TARIFF_EDIT)
    assert P.allowed(both, P.DEVICE_ADD)


def test_an_unknown_role_grants_nothing_rather_than_raising():
    """
    The database constrains the column, so an unknown value means
    somebody has been in the table by hand. The safe reading of a role
    this software has never heard of is that it permits nothing.
    """
    assert not P.allowed({"wizard"}, P.VIEW)
    assert P.permissions_of({"wizard"}) == frozenset()


def test_roles_with_lists_in_a_stable_order():
    holders = P.roles_with(P.LIMIT_SET)
    assert holders == tuple(r for r in P.ROLES if r in holders)
    assert "manager" in holders and "viewer" not in holders


# --- the honest gap -------------------------------------------------------

def test_the_permissions_with_nothing_to_guard_yet_are_known():
    """
    Declared because the specification's table declares them, not
    because anything checks them. Listing them here keeps the gap visible
    rather than forgotten - and this test fails the day one is wired up,
    which is the moment to take it off the list.
    """
    unwired = {
        P.ALARM_ACK, P.ALARM_SHELVE,     # no alarm model yet (§10 step 6)
        P.TARIFF_EDIT, P.TARGET_EDIT,    # no tariffs, no baselines
        P.RECORD_SIGN, P.RECORD_REVIEW,  # no electronic signatures yet
        P.UPDATE_APPLY,                  # the updater has no UI
        P.AUDIT_READ,                    # the audit log has no page
        P.DEVICE_EDIT,                   # nothing edits a device yet
        P.DIAGNOSTICS,                   # /healthz is unauthenticated
    }
    source = ""
    for path in (ROOT / "packages").rglob("*.py"):
        if "tests" in path.parts or "permissions.py" == path.name:
            continue
        source += path.read_text(encoding="utf-8")
    for key in unwired:
        assert f'"{key}"' not in source, (
            f"{key} is wired up now - take it off the unwired list")
