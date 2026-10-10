"""
What the poller has to do, without a network.

A fake client stands in for the Modbus connection, so these run in
milliseconds and are deterministic. The behaviour they pin down is the
behaviour that is hard to get right against a real bus: silence, recovery,
and not shouting about either.
"""

import struct
import threading

import pytest

from jetpace_core.poller import (
    DeviceSpec,
    GatewaySpec,
    GatewayWorker,
    PointSpec,
    decode_point,
    registers_needed,
)


# --- decode_point ----------------------------------------------------------

def test_decode_point_u16():
    assert decode_point(PointSpec("unit", 13, "u16"), [1]) == 1


def test_decode_point_s16_negative():
    """A reversed pressure cascade. 65531 on the wire is -5 Pa."""
    assert decode_point(PointSpec("dp", 0, "s16"), [65531]) == -5


def test_decode_point_scale():
    """EM6400 energy registers hold raw Wh, hence a 0.001 scale to kWh."""
    point = PointSpec("kwh", 120, "f32", scale=0.001)
    high, low = struct.unpack(">HH", struct.pack(">f", 1_234_567.0))
    assert decode_point(point, [high, low]) == pytest.approx(1234.567, rel=1e-6)


def test_decode_point_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown register kind"):
        decode_point(PointSpec("x", 0, "i64"), [0])


@pytest.mark.parametrize("kind, expected", [("u16", 1), ("s16", 1), ("f32", 2)])
def test_registers_needed(kind, expected):
    assert registers_needed(kind) == expected


# --- a fake bus ------------------------------------------------------------

class FakeClient:
    """Answers for the units in `served`, silent for everything else."""

    def __init__(self, served: dict[int, int]) -> None:
        self.served = served
        self.reads = 0
        self.closed = False

    def read(self, unit_id, address, count):
        self.reads += 1
        if unit_id not in self.served:
            return None                      # silence, as a real bus does
        return [self.served[unit_id]] * count

    def close(self):
        self.closed = True


class RecordingSink:
    def __init__(self) -> None:
        self.readings = []
        self.states = []
        self.lock = threading.Lock()

    def write(self, readings):
        with self.lock:
            self.readings.extend(readings)

    def device_state(self, device_code, online, detail=""):
        with self.lock:
            self.states.append((device_code, online))


def worker_for(client, *units, backoff: int = 5):
    points = (PointSpec("dp", 0, "s16"),)
    spec = GatewaySpec(
        label="t", host="x", port=1, gap_s=0.0, interval_s=0.0,
        offline_backoff_cycles=backoff,
        devices=tuple(DeviceSpec(f"DP-{u:03d}", f"room{u}", u, points)
                      for u in units),
    )
    sink = RecordingSink()
    return GatewayWorker(spec, sink, lambda _: client), sink


# --- behaviour -------------------------------------------------------------

def test_reads_served_devices_only():
    client = FakeClient({1: 12, 2: 65531})
    worker, sink = worker_for(client, 1, 2, 9)
    worker._one_pass(client)

    values = {r.device_code: r.value for r in sink.readings}
    assert values == {"DP-001": 12, "DP-002": -5}
    assert "DP-009" not in values


def test_offline_is_reported_once_not_every_cycle():
    """
    An absent device must not fill the log with one line per pass. This is
    the difference between a usable log and one nobody reads.
    """
    client = FakeClient({1: 12})
    worker, sink = worker_for(client, 1, 9)
    for _ in range(5):
        worker._one_pass(client)

    assert sink.states.count(("DP-009", False)) == 1
    assert sink.states.count(("DP-001", True)) == 1


def test_recovery_is_reported():
    """
    Backoff is switched off here so this tests only the reporting of a
    recovery. Backoff interacting with recovery is covered separately by
    test_recovered_device_clears_its_backoff.
    """
    client = FakeClient({1: 12})
    worker, sink = worker_for(client, 1, 9, backoff=0)
    worker._one_pass(client)
    assert ("DP-009", False) in sink.states

    client.served[9] = 7                     # the device comes back
    worker._one_pass(client)
    assert ("DP-009", True) in sink.states


def test_one_dead_device_does_not_stop_the_others():
    client = FakeClient({1: 12, 3: 15})
    worker, sink = worker_for(client, 1, 2, 3)
    worker._one_pass(client)

    codes = {r.device_code for r in sink.readings}
    assert codes == {"DP-001", "DP-003"}


def test_empty_pass_writes_nothing():
    """No readings means no call to the sink, not an empty batch."""
    client = FakeClient({})
    worker, sink = worker_for(client, 1, 2)
    worker._one_pass(client)
    assert sink.readings == []


# --- lifecycle -------------------------------------------------------------
#
# These exist because the first version of GatewayWorker subclassed
# threading.Thread and named an attribute `_stop`, shadowing an internal
# method that join() calls. Every test above passed, because none of them
# ever started a thread - the bug only appeared on the first real run.

def test_worker_starts_and_stops_cleanly():
    client = FakeClient({1: 12})
    points = (PointSpec("dp", 0, "s16"),)
    spec = GatewaySpec(label="t", host="x", port=1, gap_s=0.0, interval_s=0.01,
                       devices=(DeviceSpec("DP-001", "room1", 1, points),))
    sink = RecordingSink()
    worker = GatewayWorker(spec, sink, lambda _: client)

    worker.start()
    assert worker.alive
    for _ in range(200):                     # wait for at least one pass
        if sink.readings:
            break
        threading.Event().wait(0.01)
    worker.stop()
    worker.join(timeout=5.0)

    assert not worker.alive, "worker did not stop"
    assert sink.readings, "worker produced nothing"
    assert worker.cycles >= 1


def test_poller_starts_and_stops_cleanly():
    from jetpace_core.poller import Poller

    client = FakeClient({1: 12, 2: 65531})
    points = (PointSpec("dp", 0, "s16"),)
    spec = GatewaySpec(label="t", host="x", port=1, gap_s=0.0, interval_s=0.01,
                       devices=(DeviceSpec("DP-001", "a", 1, points),
                                DeviceSpec("DP-002", "b", 2, points)))
    sink = RecordingSink()
    poller = Poller([spec], sink, lambda _: client)

    poller.start()
    for _ in range(200):
        if len(sink.readings) >= 2:
            break
        threading.Event().wait(0.01)
    poller.stop()

    assert not any(w.alive for w in poller.workers)
    assert poller.stats["t"]["failures"] == 0
    values = {r.device_code: r.value for r in sink.readings}
    assert values["DP-002"] == -5


# --- offline backoff -------------------------------------------------------
#
# A dead device costs a full timeout every time it is tried. On a bus with
# thirty devices and a 2 s timeout, three dead ones consume six seconds of
# every pass - which silently destroys the scan interval for the healthy
# ones. Meril EMS solved this with offlineBackoffCycles; this is that,
# ported.

def test_dead_device_is_skipped_for_n_passes():
    client = FakeClient({1: 12})
    points = (PointSpec("dp", 0, "s16"),)
    spec = GatewaySpec(label="t", host="x", port=1, gap_s=0.0, interval_s=0.0,
                       offline_backoff_cycles=3,
                       devices=(DeviceSpec("DP-001", "live", 1, points),
                                DeviceSpec("DP-009", "dead", 9, points)))
    sink = RecordingSink()
    worker = GatewayWorker(spec, sink, lambda _: client)

    # pass 1: both tried, the dead one fails and enters backoff
    worker._one_pass(client)
    assert client.reads == 2
    assert worker.backing_off == {"DP-009": 3}

    # passes 2, 3, 4: only the live device is touched
    for expected_remaining in (2, 1, 0):
        worker._one_pass(client)
        assert worker.backing_off.get("DP-009", 0) == expected_remaining

    assert client.reads == 5, "the dead device was retried too early"
    assert worker.reads_skipped == 3

    # pass 5: backoff expired, it is tried again
    worker._one_pass(client)
    assert client.reads == 7


def test_healthy_devices_are_never_skipped():
    client = FakeClient({1: 12, 2: 13})
    points = (PointSpec("dp", 0, "s16"),)
    spec = GatewaySpec(label="t", host="x", port=1, gap_s=0.0, interval_s=0.0,
                       devices=(DeviceSpec("DP-001", "a", 1, points),
                                DeviceSpec("DP-002", "b", 2, points)))
    sink = RecordingSink()
    worker = GatewayWorker(spec, sink, lambda _: client)

    for _ in range(10):
        worker._one_pass(client)

    assert client.reads == 20
    assert worker.reads_skipped == 0
    assert worker.backing_off == {}


def test_recovered_device_clears_its_backoff():
    client = FakeClient({})
    points = (PointSpec("dp", 0, "s16"),)
    spec = GatewaySpec(label="t", host="x", port=1, gap_s=0.0, interval_s=0.0,
                       offline_backoff_cycles=2,
                       devices=(DeviceSpec("DP-001", "a", 1, points),))
    sink = RecordingSink()
    worker = GatewayWorker(spec, sink, lambda _: client)

    worker._one_pass(client)
    assert worker.backing_off == {"DP-001": 2}

    client.served[1] = 12                    # repaired
    for _ in range(3):                       # wait out the backoff
        worker._one_pass(client)

    assert worker.backing_off == {}, "backoff survived a successful read"
    assert ("DP-001", True) in sink.states
    assert sink.readings[-1].value == 12


def test_backoff_bounds_the_cost_of_a_dead_bus():
    """
    Thirty devices, three dead. Without backoff every pass pays three
    timeouts. With it, the dead ones are tried once in every six passes.
    """
    served = {u: 10 for u in range(1, 31) if u not in (7, 13, 29)}
    client = FakeClient(served)
    points = (PointSpec("dp", 0, "s16"),)
    spec = GatewaySpec(label="t", host="x", port=1, gap_s=0.0, interval_s=0.0,
                       offline_backoff_cycles=5,
                       devices=tuple(DeviceSpec(f"DP-{u:03d}", f"r{u}", u, points)
                                     for u in range(1, 31)))
    worker = GatewayWorker(spec, sink := RecordingSink(), lambda _: client)

    for _ in range(12):
        worker._one_pass(client)

    # 27 healthy devices read every pass, plus the dead ones twice each
    assert client.reads == 27 * 12 + 3 * 2
    assert worker.reads_skipped == 3 * 10
    assert len({r.device_code for r in sink.readings}) == 27
