"""
The supervisor.

Two things are tested here. One is behaviour: the health gate, the restart
backoff, the trial-and-confirm sequence. The other is a structural
property - that this module imports almost nothing - which is not a style
preference but the reason the design works at all.
"""

import ast
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from jetpace_core import supervisor as sup
from jetpace_core.supervisor import BACKOFF, ChildSpec, Supervisor
from jetpace_core.update.swap import Pending, read_pending, write_pending

SUPERVISOR_PY = Path(sup.__file__)


# --- the import constraint ------------------------------------------------
#
# A frozen executable holds its own files open. The supervisor can replace
# app/ only because nothing it has loaded lives there. That property is
# invisible, easy to break with one convenient import, and would be
# discovered on a customer's PC. So it is a test.

FORBIDDEN = {
    "psycopg", "pymodbus", "paho", "fastapi", "uvicorn", "jinja2",
    "cryptography",
}
ALLOWED_OWN = {"jetpace_core.update.swap", ".update.swap"}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    return names


def test_the_supervisor_imports_no_application_code():
    found = _imports_of(SUPERVISOR_PY)
    offenders = {n for n in found
                 for bad in FORBIDDEN
                 if n == bad or n.startswith(bad + ".")}
    assert not offenders, (
        f"{SUPERVISOR_PY.name} imports {offenders}. The supervisor must load "
        f"nothing that lives in app/, because it has to rename that folder.")


def test_the_supervisor_imports_only_one_of_our_modules():
    ours = {n for n in _imports_of(SUPERVISOR_PY)
            if n.startswith(".") or n.startswith("jetpace_core")}
    assert ours <= ALLOWED_OWN, (
        f"unexpected internal imports: {ours - ALLOWED_OWN}. Keep the "
        f"supervisor's surface small enough to reason about.")


# --- version reading ------------------------------------------------------

def test_the_version_comes_from_version_json(tmp_path):
    (tmp_path / "version.json").write_text(json.dumps({"version": "1.4.2"}))
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    assert s.current_version() == "1.4.2"


@pytest.mark.parametrize("content", ["", "not json", "{}", '{"version": null}'])
def test_a_missing_or_broken_version_file_does_not_raise(tmp_path, content):
    """
    The supervisor reads this before it can log anything useful. It must
    degrade to a placeholder, never crash - a bad version.json would
    otherwise take the whole site down.
    """
    (tmp_path / "version.json").write_text(content)
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    assert s.current_version() == "0.0.0"


def test_no_version_file_at_all(tmp_path):
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    assert s.current_version() == "0.0.0"


# --- the health gate ------------------------------------------------------

class FakeHealth:
    """A /healthz that answers however the test wants."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = 0

    def __call__(self):
        self.asked += 1
        if self.answers:
            return self.answers.pop(0)
        return True, "ok"


def test_health_is_not_just_an_open_port(monkeypatch, tmp_path):
    """
    An HTTP 200 whose body says ok=false is not healthy. This is the same
    lesson as PostgreSQL opening its socket before accepting connections:
    reachable is not ready.
    """
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    monkeypatch.setattr(s, "healthy", lambda: (False, "says it is not well"))
    monkeypatch.setattr(sup, "HEALTH_EVERY", 0.01)
    ok, why = s.wait_for_health(deadline=0.05)
    assert not ok and "not well" in why


def test_health_waits_rather_than_giving_up_at_once(monkeypatch, tmp_path):
    """A cold start is slow; a release must not be failed for being slow."""
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    probe = FakeHealth((False, "not answering"), (False, "not answering"),
                       (True, "ok"))
    monkeypatch.setattr(s, "healthy", probe)
    monkeypatch.setattr(sup, "HEALTH_EVERY", 0.01)
    ok, _ = s.wait_for_health(deadline=5.0)
    assert ok
    assert probe.asked == 3


def test_a_required_child_dying_fails_the_gate_at_once(monkeypatch, tmp_path):
    """
    No point waiting out a two-minute deadline for something that has
    already exited.
    """
    s = Supervisor(tmp_path, [ChildSpec("poller", [sys.executable, "-c", "pass"])],
                   "http://127.0.0.1:1/healthz")
    monkeypatch.setattr(s, "healthy", lambda: (False, "not answering"))
    monkeypatch.setattr(sup, "HEALTH_EVERY", 0.01)

    s._spawn(s.children[0])
    s.children[0].process.wait(timeout=30)

    start = time.monotonic()
    ok, why = s.wait_for_health(deadline=30.0)
    assert not ok
    assert "poller exited" in why
    assert time.monotonic() - start < 5          # did not wait it out


def test_an_optional_child_dying_does_not_fail_the_gate(monkeypatch, tmp_path):
    s = Supervisor(
        tmp_path,
        [ChildSpec("sim", [sys.executable, "-c", "pass"], required=False)],
        "http://127.0.0.1:1/healthz")
    monkeypatch.setattr(s, "healthy", FakeHealth((False, "wait"), (True, "ok")))
    monkeypatch.setattr(sup, "HEALTH_EVERY", 0.01)

    s._spawn(s.children[0])
    s.children[0].process.wait(timeout=30)

    ok, _ = s.wait_for_health(deadline=5.0)
    assert ok


def test_health_check_against_a_closed_port_is_not_an_exception(tmp_path):
    """A refused connection is an answer, not a crash."""
    s = Supervisor(tmp_path, [], "http://127.0.0.1:9/healthz")
    ok, why = s.healthy()
    assert not ok and why


def test_health_check_against_a_bad_url_is_not_an_exception(tmp_path):
    s = Supervisor(tmp_path, [], "http://no-such-host.invalid./healthz")
    ok, why = s.healthy()
    assert not ok and why


# --- the trial sequence ---------------------------------------------------

def _trial_pending(**kw) -> Pending:
    base = dict(from_version="1.0.0", to_version="1.1.0", staged=False,
                attempts=1, at="2026-10-12T09:00:00+00:00", by="test")
    base.update(kw)
    return Pending(**base)


def test_a_release_that_passes_its_gate_is_confirmed(tmp_path, monkeypatch):
    (tmp_path / "version.json").write_text('{"version": "1.1.0"}')
    write_pending(tmp_path, _trial_pending())

    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    monkeypatch.setattr(s, "healthy", lambda: (True, "ok"))
    monkeypatch.setattr(s, "_supervise", lambda: 0)

    assert s.run() == 0
    assert read_pending(tmp_path) is None     # the update is over


def test_a_release_that_fails_its_gate_exits_rather_than_rolling_back(
        tmp_path, monkeypatch):
    """
    The supervisor never performs the rollback itself. It exits non-zero,
    the service manager restarts it, and prepare() counts the attempt. That
    path works even when the supervisor is the broken thing - a rollback it
    had to carry out successfully would not.
    """
    (tmp_path / "version.json").write_text('{"version": "1.1.0"}')
    write_pending(tmp_path, _trial_pending(attempts=1))

    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    monkeypatch.setattr(s, "healthy", lambda: (False, "not answering"))
    monkeypatch.setattr(sup, "HEALTH_EVERY", 0.01)
    monkeypatch.setattr(sup, "HEALTH_DEADLINE", 0.05)
    monkeypatch.setattr(s, "_supervise", lambda: 0)

    assert s.run() == 1
    # prepare() counted this start on the way in, so attempts has already
    # moved 1 -> 2. What matters is that the supervisor left the record
    # alone on the way out: it did not confirm and it did not roll back.
    p = read_pending(tmp_path)
    assert p is not None
    assert p.attempts == 2
    assert p.to_version == "1.1.0"


def test_no_pending_update_means_no_health_gate(tmp_path, monkeypatch):
    """
    An ordinary restart must not be held up by a health check, and must not
    exit non-zero because the application is slow.
    """
    (tmp_path / "version.json").write_text('{"version": "1.0.0"}')
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")

    asked = {"n": 0}

    def never(self=None):
        asked["n"] += 1
        return False, "should not have been asked"

    monkeypatch.setattr(s, "healthy", never)
    monkeypatch.setattr(s, "_supervise", lambda: 0)

    assert s.run() == 0
    assert asked["n"] == 0


def test_a_staged_release_is_not_treated_as_on_trial(tmp_path, monkeypatch):
    """
    staged=true means the swap has not happened yet - which here means it
    was refused and is waiting. The running release is the old one and has
    nothing to prove.
    """
    (tmp_path / "version.json").write_text('{"version": "1.0.0"}')
    write_pending(tmp_path, _trial_pending(staged=True))
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    monkeypatch.setattr(s, "healthy",
                        lambda: pytest.fail("should not gate a staged release"))
    monkeypatch.setattr(s, "_supervise", lambda: 0)
    assert s.run() == 0


# --- children -------------------------------------------------------------

def test_a_child_writes_to_its_own_log(tmp_path):
    s = Supervisor(tmp_path, [
        ChildSpec("talker", [sys.executable, "-c", "print('hello from child')"]),
    ], "http://127.0.0.1:1/healthz", logs=tmp_path / "logs")

    s._spawn(s.children[0])
    s.children[0].process.wait(timeout=30)
    s._close(s.children[0])

    text = (tmp_path / "logs" / "talker.log").read_text(encoding="utf-8")
    assert "hello from child" in text
    assert "--- started" in text


def test_child_output_goes_to_a_file_not_a_pipe(tmp_path):
    """
    A child that writes more than the pipe buffer and is never read blocks
    forever. That is the same failure that made pg_ctl hang when its output
    was captured, so children get a file.
    """
    noisy = [sys.executable, "-c",
             "print('x' * 200)\n" * 1 + "import sys; sys.stdout.write('x'*200000)"]
    s = Supervisor(tmp_path, [ChildSpec("noisy", noisy)],
                   "http://127.0.0.1:1/healthz", logs=tmp_path / "logs")
    s._spawn(s.children[0])
    code = s.children[0].process.wait(timeout=60)
    s._close(s.children[0])
    assert code == 0            # it finished rather than blocking


def test_stopping_a_child_that_ignores_terminate_still_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(sup, "STOP_GRACE", 0.5)
    stubborn = [sys.executable, "-c",
                "import time\nwhile True: time.sleep(0.2)"]
    s = Supervisor(tmp_path, [ChildSpec("stubborn", stubborn)],
                   "http://127.0.0.1:1/healthz", logs=tmp_path / "logs")
    s._spawn(s.children[0])
    s._stop(s.children[0])
    assert s.children[0].process.poll() is not None


def test_stopping_an_already_dead_child_is_harmless(tmp_path):
    s = Supervisor(tmp_path, [ChildSpec("gone", [sys.executable, "-c", "pass"])],
                   "http://127.0.0.1:1/healthz", logs=tmp_path / "logs")
    s._spawn(s.children[0])
    s.children[0].process.wait(timeout=30)
    s._stop(s.children[0])
    s._stop(s.children[0])


def test_children_stop_in_reverse_order(tmp_path, monkeypatch):
    """
    The web tier goes first and the poller last, so nothing is mid-write
    into a database that has already been asked to stop.
    """
    order: list[str] = []
    s = Supervisor(tmp_path, [ChildSpec("poller", []), ChildSpec("ingest", []),
                              ChildSpec("web", [])],
                   "http://127.0.0.1:1/healthz")
    monkeypatch.setattr(s, "_stop", lambda c: order.append(c.spec.name))
    s.stop_all()
    assert order == ["web", "ingest", "poller"]


# --- backoff --------------------------------------------------------------

def test_backoff_grows_then_stops_growing(tmp_path):
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    waits = [s._backoff_for(n) for n in range(1, 8)]
    assert waits[:4] == list(BACKOFF)
    assert waits[4:] == [BACKOFF[-1]] * 3     # capped, never unbounded


def test_backoff_is_zero_for_a_first_start(tmp_path):
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    assert s._backoff_for(0) == 0.0
