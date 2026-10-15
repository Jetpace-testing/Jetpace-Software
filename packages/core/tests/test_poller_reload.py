"""
Changing the device list while the poller is running.

The point of the commissioning form is that a device starts being read
without anybody restarting a service, and this is the mechanism. Three
things have to be true and none of them is obvious:

  A gateway whose device list changed keeps its worker, its thread and
  its open socket. Renaming it does too. Tearing down a converter's
  connection to add a fourth device would re-walk the other thirty from
  cold, and on a slow bus that is a visible gap in every trend.

  A device that leaves takes its state with it. A stale backoff count
  would skip the first few passes of a device that comes back; a stale
  online flag would mean its return is never reported, because nothing
  appears to have changed.

  The sink forgets what it cached. This is the one that would have been
  found at a customer site: PostgresSink resolves device codes to ids
  once, at construction, and counts a reading it cannot resolve as
  `skipped`. A device commissioned while the poller ran would have been
  read off the bus perfectly and stored nowhere, with a healthy poller,
  an empty error log and a device showing offline on the dashboard.

No sockets and no database. The client factory hands out a reader that
answers from a dict.
"""

import threading
from pathlib import Path

import pytest

from jetpace_core import ingest as ingest_module
from jetpace_core.ingest import Ingest
from jetpace_core.mqtt import Site
from jetpace_core.poller import (DeviceSpec, GatewaySpec, GatewayWorker,
                                 PointSpec, Poller, Reading, Sink)
from jetpace_core.sinks import ConsoleSink, PostgresSink


def device(code="DP-001", unit_id=1, params=("dp",)) -> DeviceSpec:
    return DeviceSpec(
        code=code, name=code, unit_id=unit_id,
        points=tuple(PointSpec(param=p, address=0, kind="s16")
                     for p in params))


def gateway(host="127.0.0.1", port=5020, label="sim-01",
            devices=(), **over) -> GatewaySpec:
    return GatewaySpec(host=host, port=port, label=label,
                       devices=tuple(devices), **over)


class QuietSink:
    """Records calls; starts nothing and resolves nothing."""

    def __init__(self):
        self.readings = []
        self.states = []
        self.refreshes = 0

    def write(self, readings):
        self.readings.extend(readings)

    def device_state(self, device_code, online, detail=""):
        self.states.append((device_code, online, detail))

    def refresh(self):
        self.refreshes += 1


def never_connect(spec):
    raise AssertionError("no worker should have opened a connection here")


# --- the three outcomes ---------------------------------------------------

def test_a_new_gateway_starts():
    poller = Poller([], QuietSink(), never_connect)
    changed = poller.apply([gateway()])
    assert changed["started"] == ["sim-01"]
    assert len(poller.workers) == 1
    assert not poller.workers[0].alive, "not started: the poller has not been"


def test_a_gateway_that_disappeared_is_stopped_and_joined():
    poller = Poller([gateway()], QuietSink(), never_connect)
    worker = poller.workers[0]
    changed = poller.apply([])
    assert changed["stopped"] == ["sim-01"]
    assert poller.workers == []
    assert worker._stopping.is_set()


def test_the_same_gateway_with_a_new_device_keeps_its_worker():
    """
    Identity, not equality. A new GatewayWorker here would mean a new
    thread and a new socket to the converter, and thirty devices walked
    from cold to add one.
    """
    poller = Poller([gateway(devices=[device("DP-001", 1)])],
                    QuietSink(), never_connect)
    before = poller.workers[0]
    changed = poller.apply([gateway(devices=[device("DP-001", 1),
                                             device("DP-002", 2)])])
    assert changed["retargeted"] == ["sim-01"]
    assert poller.workers[0] is before
    assert [d.code for d in before.spec.devices] == ["DP-001", "DP-002"]


def test_renaming_a_gateway_does_not_restart_it():
    """
    Identity is (host, port), which is what UNIQUE (tenant_id, host,
    port) says in migrations/0001 and what the physical converter
    actually is. Relabelling "sim-01" to "Block A" is a UI change.
    """
    poller = Poller([gateway(label="sim-01")], QuietSink(), never_connect)
    before = poller.workers[0]
    changed = poller.apply([gateway(label="Block A")])
    assert changed["retargeted"] == ["Block A"]
    assert changed["started"] == [] and changed["stopped"] == []
    assert poller.workers[0] is before


def test_moving_a_gateway_to_a_new_address_is_a_different_gateway():
    """
    A converter reached on a new IP is a new TCP connection whichever way
    it is modelled, so this one really is a stop and a start.
    """
    poller = Poller([gateway(host="127.0.0.1")], QuietSink(), never_connect)
    changed = poller.apply([gateway(host="10.0.0.9")])
    assert changed["stopped"] == ["sim-01"] and changed["started"] == ["sim-01"]


def test_an_unchanged_gateway_is_not_touched_at_all():
    """
    The overwhelmingly common case - every reload where nothing was
    commissioned - and it has to be free.
    """
    spec = gateway(devices=[device()])
    poller = Poller([spec], QuietSink(), never_connect)
    before = poller.workers[0]
    changed = poller.apply([gateway(devices=[device()])])
    assert changed == {"started": [], "retargeted": [], "stopped": []}
    assert poller.workers[0] is before
    assert before.spec is spec, "the spec object was replaced for no reason"


def test_a_changed_timing_retargets_without_restarting():
    poller = Poller([gateway(interval_s=2.0)], QuietSink(), never_connect)
    before = poller.workers[0]
    poller.apply([gateway(interval_s=5.0)])
    assert poller.workers[0] is before
    assert before.spec.interval_s == 5.0


def test_the_worker_order_follows_the_incoming_list():
    """
    So `stats` and the log line read in the same order as the device list
    an operator is looking at.
    """
    a, b, c = (gateway(host="10.0.0.1", label="a"),
               gateway(host="10.0.0.2", label="b"),
               gateway(host="10.0.0.3", label="c"))
    poller = Poller([c, a], QuietSink(), never_connect)
    poller.apply([a, b, c])
    assert [w.spec.label for w in poller.workers] == ["a", "b", "c"]


def test_applying_to_a_running_poller_starts_the_new_worker():
    """
    And not applying to a stopped one, which is the difference the
    `_started` flag carries: `apply` is called both before `start` (from
    construction in cmd_poller) and after it (from the reload loop).
    """
    poller = Poller([], QuietSink(), lambda spec: _NullReader())
    poller.start()
    try:
        poller.apply([gateway(devices=[device()])])
        assert poller.workers[0].alive
    finally:
        poller.stop()


class _NullReader:
    def read(self, unit_id, address, count):
        return None

    def close(self):
        pass


# --- state that must not survive a device leaving -------------------------

def test_a_departed_device_leaves_no_backoff_behind():
    """
    A device moved to another gateway and moved back would otherwise be
    skipped for its old backoff count on return.
    """
    worker = GatewayWorker(gateway(devices=[device("DP-001", 1),
                                            device("DP-002", 2)]),
                           QuietSink(), never_connect)
    worker._backoff["DP-002"] = 5
    worker._online["DP-002"] = False
    worker.retarget(gateway(devices=[device("DP-001", 1)]))
    assert "DP-002" not in worker._backoff
    assert "DP-002" not in worker._online


def test_a_device_that_stayed_keeps_its_state():
    """
    The other half. Clearing everything on every reload would re-report
    every device's state each time, which on a site with a few dead ones
    is a log line every ten seconds saying nothing has changed.
    """
    worker = GatewayWorker(gateway(devices=[device("DP-001", 1)]),
                           QuietSink(), never_connect)
    worker._backoff["DP-001"] = 3
    worker._online["DP-001"] = True
    worker.retarget(gateway(devices=[device("DP-001", 1),
                                     device("DP-002", 2)]))
    assert worker._backoff["DP-001"] == 3
    assert worker._online["DP-001"] is True


# --- the sink forgets what it cached --------------------------------------

def postgres_sink_without_a_database(devices, params):
    """
    A PostgresSink with its lookups preloaded and no connection.

    Built without __init__ deliberately: the defect under test is in the
    caching, which is three dicts and a lock, and standing up PostgreSQL
    to exercise it would mean this never runs on a build machine.
    """
    sink = object.__new__(PostgresSink)
    sink._lock = threading.Lock()
    sink._device_ids = dict(devices)
    sink._param_ids = dict(params)
    sink.written = 0
    sink.skipped = 0
    sink._conn = None
    return sink


def test_a_reading_for_an_unknown_device_is_counted_not_invented():
    """
    The existing behaviour, and the right one: commissioning a device is
    a deliberate act, not a side effect of it answering on a bus.
    """
    sink = postgres_sink_without_a_database({"DP-001": 1}, {"dp": 1})
    sink.write([Reading(ts=None, device_code="DP-009", param="dp", value=1.0)])
    assert sink.skipped == 1 and sink.written == 0


def test_refresh_re_resolves_the_codes_a_commissioned_device_brought():
    """
    The defect this method exists for. Before it, every reading from a
    device commissioned while the poller ran was counted as `skipped` -
    forever, with a healthy-looking poller and nothing in the logs.
    """
    sink = postgres_sink_without_a_database({"DP-001": 1}, {"dp": 1})
    rows = []

    class Cur:
        def __init__(self):
            self.answers = [[("DP-001", 1), ("DP-009", 9)], [("dp", 1)]]

        def execute(self, sql, params=None):
            rows.append(sql)

        def fetchall(self):
            return self.answers.pop(0)

        def executemany(self, sql, values):
            rows.extend(values)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Conn:
        def cursor(self):
            return Cur()

    sink._conn = Conn()
    sink.refresh()
    assert sink._device_ids == {"DP-001": 1, "DP-009": 9}

    sink.write([Reading(ts=None, device_code="DP-009", param="dp", value=1.0)])
    assert sink.written == 1 and sink.skipped == 0


def test_every_sink_answers_refresh():
    """
    It is on the Sink Protocol, which is duck-typed and unenforced (debt
    #2), so a sink that did not implement it would fail at a site the
    first time a device was commissioned.
    """
    assert hasattr(Sink, "refresh")
    for sink in (ConsoleSink(quiet=True),
                 postgres_sink_without_a_database({}, {})):
        assert callable(getattr(sink, "refresh", None))
    # MqttSink is not constructed here - it connects to a broker - so its
    # method is checked on the class.
    from jetpace_core.sinks import MqttSink
    assert callable(MqttSink.refresh)


# --- the same defect on the MQTT path -------------------------------------

def test_ingest_re_resolves_when_a_reading_arrives_for_an_unknown_device(
        monkeypatch):
    """
    MqttSink publishes a device CODE and this process resolves it, so it
    has the identical stale-cache defect and no reload loop to hang a fix
    on. It reacts to the symptom instead: readings being skipped is
    exactly what an uncommissioned code looks like.
    """
    monkeypatch.setattr(ingest_module, "BATCH_SIZE", 1)
    monkeypatch.setattr(ingest_module, "UNKNOWN_RECHECK_SECONDS", 0.0)

    sink = QuietSink()
    sink.skipped = 0

    def write(readings):
        sink.skipped += len(readings)      # every one unresolvable

    sink.write = write

    ingest = Ingest(Site(tenant="t", site="s"), sink)
    ingest.queue.put(Reading(ts=None, device_code="DP-009", param="dp",
                             value=1.0))
    ingest.stopping.set()
    ingest.drain()
    assert ingest.refreshes == 1
    assert sink.refreshes == 1


def test_ingest_does_not_re_resolve_when_nothing_is_being_skipped(monkeypatch):
    """
    The rate limit's other half. A shared broker carrying a second site's
    topics produces unknown codes that never resolve, and without the
    floor that would be two queries per batch forever.
    """
    monkeypatch.setattr(ingest_module, "BATCH_SIZE", 1)
    monkeypatch.setattr(ingest_module, "UNKNOWN_RECHECK_SECONDS", 0.0)

    sink = QuietSink()
    sink.skipped = 0
    ingest = Ingest(Site(tenant="t", site="s"), sink)
    ingest.queue.put(Reading(ts=None, device_code="DP-001", param="dp",
                             value=1.0))
    ingest.stopping.set()
    ingest.drain()
    assert ingest.refreshes == 0 and sink.refreshes == 0


# --- the orphan class of fault --------------------------------------------

def test_refresh_reports_a_device_id_that_changed_underneath_it():
    """
    FOUND LIVE, after a superuser purge and the same codes commissioned
    again.

    The reactive refresh only fires on an UNKNOWN code. A code whose
    numeric id CHANGED resolves perfectly well - to a row that is gone -
    so nothing was skipped, nothing was refreshed, and 174 readings went
    into the historian against a deleted device id. `reading` has no
    foreign key on device_id (0001: it is a hypertable), so the database
    accepted every one, and every query joins `device`, so none of them
    appears anywhere.

    One warning is the only notice anybody gets, so it has to happen.
    """
    sink = postgres_sink_without_a_database({"DP-001": 1, "DP-002": 2},
                                            {"dp": 1})

    class Cur:
        def __init__(self):
            # DP-001 came back with a new id; DP-002 is unchanged.
            self.answers = [[("DP-001", 9), ("DP-002", 2)], [("dp", 1)]]

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return self.answers.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Conn:
        def cursor(self):
            return Cur()

    sink._conn = Conn()
    moved = sink.refresh()
    assert moved == {"DP-001": (1, 9)}
    assert "DP-002" not in moved, "an unchanged id is not news"


def test_ingest_re_resolves_on_age_as_well_as_on_evidence():
    """
    The two triggers catch different faults. On evidence: an unknown
    code, which is a device commissioned a moment ago. On age: nothing
    looks wrong at all, which is exactly how a changed id behaves.

    Without the second, a cache poisoned by a purge stays poisoned until
    the process restarts.
    """
    source = Path(ingest_module.__file__).read_text(encoding="utf-8")
    assert "LOOKUP_MAX_AGE_SECONDS" in source
    assert ingest_module.LOOKUP_MAX_AGE_SECONDS <= 600, (
        "a longer window than ten minutes is a long time to write "
        "readings nobody will ever see")


def test_ingest_refreshes_on_age_with_nothing_being_skipped(monkeypatch):
    monkeypatch.setattr(ingest_module, "BATCH_SIZE", 1)
    monkeypatch.setattr(ingest_module, "UNKNOWN_RECHECK_SECONDS", 1e9)
    monkeypatch.setattr(ingest_module, "LOOKUP_MAX_AGE_SECONDS", 0.0)

    sink = QuietSink()
    sink.skipped = 0
    ingest = Ingest(Site(tenant="t", site="s"), sink)
    ingest.queue.put(Reading(ts=None, device_code="DP-001", param="dp",
                             value=1.0))
    ingest.stopping.set()
    ingest.drain()
    assert ingest.refreshes == 1, "nothing was skipped; age alone must do it"
