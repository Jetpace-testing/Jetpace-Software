"""
The first write path this application has ever had.

Everything here runs against a scripted cursor rather than a database:
the questions worth asking are about what SQL is sent, what is refused
before any SQL is sent at all, and whether the audit row can be left out.
None of those need PostgreSQL, and a test that needs PostgreSQL does not
run on a build machine.

What is deliberately NOT tested here is that the CHECK constraints work -
that is the database's job and the database does it. These tests cover the
layer in front, whose only purpose is that an operator reads a sentence
instead of a constraint name.
"""

from pathlib import Path

import pytest

from jetpace_core.web import queries, writes

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "migrations" / "0005_limits_and_audit.sql"
SETUP = ROOT / "scripts" / "setup_postgres.py"


# --- a cursor that remembers what it was asked ----------------------------

class FakeCursor:
    """
    Records every statement, and answers fetchone() from a script.

    The script is a list of rows handed out in order, which is enough for
    every path here: resolve the device, resolve the parameter, read the
    previous limit, insert, insert the audit row.
    """

    def __init__(self, script: list[dict | None]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params or {}))

    def fetchone(self):
        return self.script.pop(0) if self.script else None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, script: list[dict | None]) -> None:
        self.cur = FakeCursor(script)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.cur.calls]

    def params_for(self, fragment: str) -> dict:
        for sql, params in self.cur.calls:
            if fragment in sql:
                return params
        raise AssertionError(f"no statement containing {fragment!r} in "
                             f"{self.statements}")


DEVICE = {"id": 7, "tenant_id": 1, "retired_at": None}
PARAM = {"id": 3}
PREVIOUS = {"low": -5.0, "high": -1.0}
INSERTED = {"id": 42, "set_at": "2026-10-15T10:00:00+05:30"}
AUDITED = {"id": 99, "at": "2026-10-15T10:00:00+05:30"}


def conn_for_a_successful_limit(previous=PREVIOUS) -> FakeConn:
    return FakeConn([DEVICE, PARAM, previous, INSERTED, AUDITED])


# --- what is refused before the database is touched -----------------------

def test_a_limit_needs_at_least_one_set_point():
    """
    Both sides blank is not "no limits", it is a row that means nothing.
    The database says so too; this says it in words.
    """
    conn = FakeConn([])
    with pytest.raises(writes.WriteRejected) as exc:
        writes.set_limit(conn, device_code="DP-001", param="dp",
                         low=None, high=None)
    assert "low or a high" in str(exc.value)
    assert conn.statements == [], "refused before any SQL was sent"


def test_the_low_set_point_must_be_below_the_high_one():
    conn = FakeConn([])
    with pytest.raises(writes.WriteRejected) as exc:
        writes.set_limit(conn, device_code="DP-001", param="dp",
                         low=10, high=5)
    assert exc.value.field == "low"
    # the numbers the operator typed, in the message, so they do not have
    # to go back to the form to see what was wrong
    assert "10" in exc.value.message and "5" in exc.value.message
    assert conn.statements == []


def test_equal_set_points_are_refused_and_not_stored_as_a_band():
    """
    A band of zero width makes every reading an excursion. Cheaper to
    refuse than to diagnose from the resulting wall of alarms.
    """
    with pytest.raises(writes.WriteRejected):
        writes.set_limit(FakeConn([]), device_code="DP-001", param="dp",
                         low=5, high=5)


def test_a_blank_field_means_no_limit_on_that_side_and_not_zero():
    """
    The one that would be invisible if it were wrong.

    An HTML form posts "" for a field nobody filled in. Treating that as
    0 would put a plausible set point on a pressure cascade - zero Pa is
    exactly the boundary a cascade is specified around - so a device
    commissioned with an empty HSP would silently acquire a ceiling of
    zero and alarm on every normal positive reading.
    """
    conn = conn_for_a_successful_limit()
    writes.set_limit(conn, device_code="DP-001", param="dp",
                     low="-8", high="   ")
    params = conn.params_for("INSERT INTO device_limit")
    assert params["low"] == -8.0
    assert params["high"] is None


def test_an_unparseable_set_point_is_a_sentence():
    with pytest.raises(writes.WriteRejected) as exc:
        writes.set_limit(FakeConn([]), device_code="DP-001", param="dp",
                         low="minus eight", high=None)
    assert "must be a number" in str(exc.value)


def test_an_unknown_device_is_a_sentence_not_a_foreign_key_violation():
    conn = FakeConn([None])          # the device lookup finds nothing
    with pytest.raises(writes.WriteRejected) as exc:
        writes.set_limit(conn, device_code="NOPE", param="dp",
                         low=-8, high=-1)
    assert "NOPE" in str(exc.value)
    assert "INSERT INTO device_limit" not in " ".join(conn.statements)


def test_an_unknown_parameter_is_a_sentence():
    conn = FakeConn([DEVICE, None])
    with pytest.raises(writes.WriteRejected) as exc:
        writes.set_limit(conn, device_code="DP-001", param="humidity",
                         low=30, high=60)
    assert "humidity" in str(exc.value)


# --- the audit row cannot be left out -------------------------------------

def test_a_limit_records_itself_in_the_audit_log():
    """
    Not a courtesy from the caller. The audit INSERT comes from inside
    set_limit, in the same transaction, so there is no call sequence that
    changes a set point without recording it.
    """
    conn = conn_for_a_successful_limit()
    writes.set_limit(conn, device_code="DP-001", param="dp",
                     low=-8, high=-1, reason="commissioned")
    joined = " ".join(conn.statements)
    assert "INSERT INTO device_limit" in joined
    assert "INSERT INTO audit" in joined


def test_the_audit_row_says_what_the_limit_was_before():
    """
    "It is now -8" is much less useful in a year than "it was -5 and
    somebody made it -8".
    """
    conn = conn_for_a_successful_limit()
    writes.set_limit(conn, device_code="DP-001", param="dp",
                     low=-8, high=-1)
    detail = conn.params_for("INSERT INTO audit")["detail"].obj
    assert detail["previous_low"] == -5.0
    assert detail["previous_high"] == -1.0
    assert detail["low"] == -8.0


def test_the_first_limit_a_device_gets_has_no_previous_one():
    """
    Null rather than zero, so "commissioned with these set points" is
    distinguishable from "somebody changed them afterwards".
    """
    conn = conn_for_a_successful_limit(previous=None)
    writes.set_limit(conn, device_code="DP-001", param="dp", low=-8, high=-1)
    detail = conn.params_for("INSERT INTO audit")["detail"].obj
    assert detail["previous_low"] is None
    assert detail["previous_high"] is None


def test_the_actor_is_never_invented():
    """
    There is no user model (§10), so an acknowledgement or a change has
    nobody to attribute it to. A null says "this install had no users",
    which is true; a string like "dashboard" or "system" would be a name
    somebody could later mistake for a person.
    """
    conn = conn_for_a_successful_limit()
    writes.set_limit(conn, device_code="DP-001", param="dp", low=-8, high=-1)
    assert conn.params_for("INSERT INTO device_limit")["set_by"] is None
    assert conn.params_for("INSERT INTO audit")["actor"] is None


def test_a_rejected_value_still_reaches_the_audit_detail_when_recorded():
    """
    `record` stores the detail as given. An audit log that only holds what
    succeeded cannot answer "did somebody try".
    """
    conn = FakeConn([AUDITED])
    writes.record(conn, tenant_id=1, action="device.add", subject="DP-009",
                  detail={"unit_id": 300, "rejected": "slave id out of range"})
    detail = conn.params_for("INSERT INTO audit")["detail"].obj
    assert detail["unit_id"] == 300
    assert "rejected" in detail


# --- the shape of the module ----------------------------------------------

def test_nothing_in_writes_commits():
    """
    The caller's connection context manager owns the transaction. A commit
    in the middle of this module would turn "the device, its parameters,
    its limits and the audit row, or none of them" into four separate
    outcomes, and the partial ones are the dangerous ones.
    """
    source = Path(writes.__file__).read_text(encoding="utf-8")
    assert ".commit()" not in source
    assert "autocommit" not in source


def test_a_limit_is_only_ever_inserted():
    """
    The application role has no UPDATE or DELETE on device_limit, so a
    statement attempting one would fail at the site rather than in a test.
    Catching it here is the cheap half.
    """
    source = Path(writes.__file__).read_text(encoding="utf-8").upper()
    assert "UPDATE DEVICE_LIMIT" not in source
    assert "DELETE FROM DEVICE_LIMIT" not in source
    assert "UPDATE AUDIT" not in source
    assert "DELETE FROM AUDIT" not in source


def test_the_write_module_is_the_only_one_that_writes():
    """
    `queries.py` says in its docstring that all of it reads. This is what
    keeps that true as the file grows.
    """
    source = Path(queries.__file__).read_text(encoding="utf-8").upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        # mentions in prose are fine; a statement is not
        for line in source.splitlines():
            if line.strip().startswith(verb):
                raise AssertionError(f"queries.py writes: {line.strip()}")


# --- the migration and the grant that makes it append-only ----------------

def test_the_migration_records_itself_under_its_own_version():
    """
    The runner takes the version from the filename and skips what
    schema_migrations already has. A file whose INSERT disagrees with its
    name would be applied on every single start.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "VALUES (5, '0005_limits_and_audit')" in sql
    assert "ON CONFLICT (version) DO NOTHING" in sql


def test_the_migration_is_one_transaction():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("BEGIN;") == 1 and sql.count("COMMIT;") == 1


def test_both_new_tables_are_append_only_for_the_application():
    """
    ALTER DEFAULT PRIVILEGES in the same grant script hands every future
    table SELECT, INSERT, UPDATE and DELETE. Without an explicit revoke,
    a migration adding an audit log would produce an audit log the
    audited application can rewrite.
    """
    setup = SETUP.read_text(encoding="utf-8")
    assert "REVOKE UPDATE, DELETE ON device_limit" in setup
    assert "REVOKE UPDATE, DELETE ON audit" in setup


def test_the_revoke_survives_a_database_that_is_behind():
    """
    psql runs with ON_ERROR_STOP=1, so a revoke on a table that does not
    exist yet would abort the whole grant script and leave the role with
    nothing.
    """
    setup = SETUP.read_text(encoding="utf-8")
    assert "to_regclass('public.device_limit')" in setup
    assert "to_regclass('public.audit')" in setup


def test_there_is_one_command_that_applies_this_to_an_existing_install():
    """
    The full setup path runs migrate() then grant_privileges(). An
    existing install needs exactly those two and none of the download,
    initdb or create-database steps.
    """
    setup = SETUP.read_text(encoding="utf-8")
    assert '"--migrate"' in setup
    migrate_block = setup.split('if args.migrate:')[1].split('\n\n')[0]
    assert "migrate()" in migrate_block
    assert "grant_privileges()" in migrate_block


# --- reading a limit back -------------------------------------------------

def test_the_limit_in_force_is_the_latest_row_and_not_a_flag():
    """
    There is no `current` column and no `superseded_at`, because both
    would need the UPDATE the grant withholds. The ordering is the state,
    so every read has to be a latest-per-group.
    """
    for sql in (queries.LIMITS, queries.DEVICE_LIMITS):
        assert "DISTINCT ON" in sql
        assert "set_at DESC" in sql


def test_the_overview_limits_query_leaves_out_retired_devices():
    assert "retired_at IS NULL" in queries.LIMITS


def test_a_retired_device_still_shows_what_it_was_held_to():
    """
    Reached from the device's own page, where the history is the point.
    """
    assert "retired_at" not in queries.DEVICE_LIMITS
