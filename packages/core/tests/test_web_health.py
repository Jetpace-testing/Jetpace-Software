"""
The release gate.

/healthz is what the supervisor polls while a swapped-in release is on
trial, and what the installer waits for before calling an install
finished. It is the only thing standing between a broken release and
being confirmed as good, so its failure modes matter more than its
successes.

It used to ask whether any reading existed in the last 24 hours. These
tests pin the two ways that was wrong - it stayed green for a day after
logging stopped, and it would have been satisfied by the previous
version's data two seconds after a swap - and the two callers that need
an empty system to count as well.

BOTH INPUTS ARE STATED, never left to timing. A verdict here depends on
the reading's age AND on how long the process has been up, so a test
that supplied only the age would be asserting against whatever the real
clock happened to do between constructing the app and making the
request - which is how the first draft of this file produced two
failures that were not bugs. `ask` takes both.

No database and no sleeping: the SQL is not under test, the judgement is.
"""

import time

import pytest
from fastapi.testclient import TestClient

from jetpace_core.web import app as app_module
from jetpace_core.web import queries
from jetpace_core.web.app import create_app, reading_age_limit

DSN = "postgresql://unused:unused@127.0.0.1:1/unused"


@pytest.fixture
def ask(monkeypatch):
    """
    Put a question to /healthz with both inputs pinned.

    The clock is SHIFTED rather than frozen - every reading is still the
    real monotonic clock plus an offset - so it stays monotonic for
    anything else that consults it during the request, while letting the
    process appear to have been up for as long as the test needs.
    """
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def question(age_s, uptime_s=60.0, devices=3, newest=None):
        monkeypatch.setattr(queries, "connect", lambda dsn: _Conn())
        monkeypatch.setattr(queries, "health",
                            lambda conn: {"age_s": age_s,
                                          "devices": devices,
                                          "newest": newest})
        real = time.monotonic
        offset = {"by": 0.0}
        monkeypatch.setattr(app_module.time, "monotonic",
                            lambda: real() + offset["by"])

        client = TestClient(create_app(DSN), raise_server_exceptions=False)
        offset["by"] = uptime_s          # the process ages, instantly
        return client.get("/healthz")

    return question


# --- the bug that was reported --------------------------------------------

def test_logging_that_stopped_an_hour_ago_is_not_well(ask):
    """
    THE REPORTED BUG. On 13 October readings stopped at 16:55 and this
    endpoint reported ok:true for the next half hour, because 67,000
    readings were still inside its 24-hour window. A health check that
    counts a day of history cannot notice a day of silence.

    A long-lived process, so the since-start check is satisfied and the
    age is the only thing left to judge - which is exactly the shape of
    the incident.
    """
    response = ask(age_s=3600.0, uptime_s=10_000.0)
    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "stopped logging" in response.json()["why"]


@pytest.mark.parametrize("age_s", [121.0, 600.0, 6 * 3600.0, 23 * 3600.0])
def test_anything_past_the_limit_is_not_well_however_recent_the_day_was(
        ask, age_s):
    """
    Every one of these sits inside the old 24-hour window and used to
    pass. 23 hours is the extreme case: a system silent since yesterday,
    reported well.
    """
    assert ask(age_s=age_s, uptime_s=24 * 3600.0).status_code == 503


def test_a_reading_inside_the_limit_is_well(ask):
    response = ask(age_s=30.0, uptime_s=10_000.0)
    assert response.status_code == 200
    assert response.json()["why"] == "ok"


# --- the bug that would have survived a naive fix -------------------------

def test_the_previous_versions_data_does_not_satisfy_the_gate(ask):
    """
    THE SECOND BUG, and the reason a freshness check alone is not the
    fix. The gate runs seconds after a release is swapped in, when the
    readings the OLD version wrote are still fresh. A broken release that
    never logs anything would pass on its first poll, on the strength of
    data it did not write.

    A reading older than our own uptime was written before we started, so
    it cannot be ours - and two seconds is exactly when the supervisor
    first asks.
    """
    response = ask(age_s=5.0, uptime_s=2.0)
    assert response.status_code == 503
    assert "since this process started" in response.json()["why"]


def test_a_reading_that_arrived_after_we_started_is_ours(ask):
    response = ask(age_s=1.0, uptime_s=30.0)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_the_two_checks_are_independent(ask):
    """
    Fresh but not ours, and ours but not fresh, both fail - for
    different stated reasons. If either check subsumed the other, one of
    these would pass.
    """
    not_ours = ask(age_s=3.0, uptime_s=1.0)
    stale = ask(age_s=900.0, uptime_s=10_000.0)
    assert not_ours.status_code == 503
    assert stale.status_code == 503
    assert not_ours.json()["why"] != stale.json()["why"]


# --- an empty system is well, and two callers need it to be ---------------

def test_a_system_that_has_never_logged_is_well(ask):
    """
    Required by the installer: wait_for_dashboard() in
    scripts/provision_entry.py polls for ok:true against a database that
    is empty by definition, and would otherwise report a failed install
    after two minutes of a perfectly good one.

    Required again by a site that is installed but not commissioned,
    which must still be able to take an update. Nothing can regress from
    nothing.
    """
    response = ask(age_s=None, uptime_s=1.0, devices=0)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "nothing" in response.json()["why"]


def test_devices_configured_but_nothing_logged_yet_is_still_well(ask):
    """
    The commissioning window: the device list is seeded, the converter is
    not plugged in yet. Unwell would be defensible, but it would mean a
    site cannot be updated until its instruments answer, and this gate's
    job is to catch a regression rather than to audit the plant.
    """
    assert ask(age_s=None, uptime_s=10_000.0, devices=12).status_code == 200


# --- the database itself --------------------------------------------------

def test_a_database_that_will_not_answer_is_not_well(monkeypatch):
    def boom(dsn):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(queries, "connect", boom)
    client = TestClient(create_app(DSN), raise_server_exceptions=False)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "connection refused" in response.json()["why"]


# --- the limit ------------------------------------------------------------

def test_the_limit_sits_above_the_offline_threshold():
    """
    OFFLINE_AFTER is 90 seconds and is already the generous answer to
    "has this device gone quiet", generous because a device in offline
    backoff is skipped for several scan passes. This answers the bigger
    question - has EVERYTHING gone quiet - so it must not be tighter.
    """
    assert app_module.READING_AGE_LIMIT >= 90.0


def test_the_limit_can_be_raised_for_a_slow_scan_class(monkeypatch):
    """
    A site polling a meter every three minutes would otherwise be called
    unwell between readings.
    """
    monkeypatch.setenv(app_module.ENV_AGE_LIMIT, "600")
    assert reading_age_limit() == 600.0


def test_a_raised_limit_actually_changes_the_verdict(ask, monkeypatch):
    """
    Not just the getter - the endpoint has to consult it. A setting that
    reads back correctly and is then ignored is the worse bug.
    """
    assert ask(age_s=300.0, uptime_s=10_000.0).status_code == 503
    monkeypatch.setenv(app_module.ENV_AGE_LIMIT, "600")
    assert ask(age_s=300.0, uptime_s=10_000.0).status_code == 200


@pytest.mark.parametrize("value", ["0", "-5", "nonsense", "", " "])
def test_a_nonsense_limit_is_ignored_rather_than_obeyed(monkeypatch, value):
    """
    A limit of zero would mark every system unwell for ever, and during
    an update that means rolling back a release that was fine. Refusing
    the value is safer than honouring it.
    """
    monkeypatch.setenv(app_module.ENV_AGE_LIMIT, value)
    assert reading_age_limit() == app_module.READING_AGE_LIMIT


def test_the_limit_is_read_at_call_time_not_at_import(monkeypatch):
    """
    The HEALTH_DEADLINE lesson: a threshold captured at import cannot be
    moved by a test without the test paying for it in wall-clock seconds.
    """
    monkeypatch.setenv(app_module.ENV_AGE_LIMIT, "7")
    assert reading_age_limit() == 7.0
    monkeypatch.setenv(app_module.ENV_AGE_LIMIT, "9")
    assert reading_age_limit() == 9.0


# --- what the body says ---------------------------------------------------

def test_the_body_reports_the_numbers_behind_the_verdict(ask):
    """
    Whoever reads this at 2am needs the figures, not just a boolean - and
    the supervisor logs the whole body when the gate fails.
    """
    body = ask(age_s=1.0, uptime_s=30.0, devices=7).json()
    for field in ("ok", "why", "age_s", "uptime_s", "age_limit_s", "devices"):
        assert field in body, f"{field} missing from the health body"
    assert body["devices"] == 7
    assert body["age_limit_s"] == app_module.READING_AGE_LIMIT


def test_the_verdict_and_the_status_code_never_disagree(ask):
    """
    The supervisor checks status 200 AND ok is True; so does the
    installer. The two must not be able to contradict each other.
    """
    for age_s, uptime_s in ((None, 1.0), (1.0, 30.0), (5.0, 2.0),
                            (3600.0, 10_000.0)):
        response = ask(age_s=age_s, uptime_s=uptime_s)
        assert (response.status_code == 200) is (response.json()["ok"] is True)
