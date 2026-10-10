"""
Reading devices, on a schedule.

The unit of concurrency here is the GATEWAY, not the device. Modbus over a
shared RS-485 pair is sequential by nature - one request at a time, because
the devices share two wires - so fifteen gateways means fifteen workers,
each walking its own devices in turn. Four hundred meters does not mean
four hundred anything.

That is why this uses plain threads and blocking calls rather than asyncio.
Fifteen threads is a trivial requirement, the work is I/O-bound so the GIL
is irrelevant, and it keeps Python's split between synchronous and async
libraries out of the entire codebase.

Everything the poller produces goes to a Sink. There are two
implementations - write straight to the database, or publish to MQTT - and
the poller cannot tell which it has. That seam is what lets the transport
change without the acquisition code knowing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from .decode import decode_float32, decode_s16, decode_u16


# ---------------------------------------------------------------------------
# what flows through the system
# ---------------------------------------------------------------------------

QUALITY_GOOD = 0
QUALITY_SUBSTITUTED = 1
QUALITY_SUSPECT = 2


@dataclass(frozen=True)
class Reading:
    """One value, from one device, at one moment."""

    ts: datetime
    device_code: str
    param: str
    value: float
    quality: int = QUALITY_GOOD


class Sink(Protocol):
    """Where readings go. The poller knows nothing else about the world."""

    def write(self, readings: list[Reading]) -> None: ...

    def device_state(self, device_code: str, online: bool, detail: str = "") -> None:
        """Called when a device starts or stops answering."""
        ...


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PointSpec:
    """One value to read from one device."""

    param: str
    address: int          # wire address: datasheet number - 40001
    kind: str             # 's16' | 'u16' | 'f32'
    scale: float = 1.0


@dataclass(frozen=True)
class DeviceSpec:
    code: str
    name: str
    unit_id: int          # Modbus slave address
    points: tuple[PointSpec, ...]


@dataclass(frozen=True)
class GatewaySpec:
    label: str
    host: str
    port: int = 502
    timeout_s: float = 2.0
    gap_s: float = 0.025          # between requests on the same bus
    interval_s: float = 1.0       # one full pass of all devices
    # How many passes to skip a device that did not answer. A dead device
    # costs a full timeout every time it is tried, and on a bus of thirty
    # devices a handful of dead ones can consume the entire scan interval.
    # Skipping them for a few passes keeps the healthy devices on schedule.
    #
    # FIXED, not exponential, and deliberately so: a device that is
    # repaired must be noticed within a bounded time. Exponential backoff
    # would eventually retry so rarely that a working device stays marked
    # dead for hours.
    offline_backoff_cycles: int = 5
    devices: tuple[DeviceSpec, ...] = ()


# ---------------------------------------------------------------------------
# decoding a point
# ---------------------------------------------------------------------------

def decode_point(spec: PointSpec, registers: list[int]) -> float:
    """Turn the registers a device returned into the number it means."""
    if spec.kind == "u16":
        raw: float = decode_u16(registers[0])
    elif spec.kind == "s16":
        raw = decode_s16(registers[0])
    elif spec.kind == "f32":
        raw = decode_float32(registers[0], registers[1])
    else:
        raise ValueError(f"unknown register kind {spec.kind!r}")
    return raw * spec.scale


def registers_needed(kind: str) -> int:
    return 2 if kind == "f32" else 1


# ---------------------------------------------------------------------------
# one gateway, one thread
# ---------------------------------------------------------------------------

class GatewayWorker:
    """
    Walks one gateway's devices in turn, forever, until told to stop.

    A device that does not answer is reported offline once - not on every
    cycle - and is still retried on the next pass. Silence is the normal
    failure on a serial bus: a real master times out, it does not get an
    error back, which is why the simulator stays silent for an absent slave.

    This HOLDS a thread rather than subclassing Thread. Subclassing works
    until an attribute name collides with one of Thread's internals - and
    `_stop` is one of them, which join() calls. Composition makes that
    class of bug impossible.
    """

    def __init__(self, spec: GatewaySpec, sink: Sink, client_factory) -> None:
        self.spec = spec
        self.sink = sink
        self.client_factory = client_factory
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"gw-{spec.label}", daemon=True)
        self._online: dict[str, bool] = {}
        # device code -> passes still to skip before trying it again
        self._backoff: dict[str, int] = {}
        self.cycles = 0
        self.failures = 0
        self.reads_skipped = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _loop(self) -> None:
        client = None
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                if client is None:
                    client = self.client_factory(self.spec)
                self._one_pass(client)
            except Exception as exc:          # noqa: BLE001 - a bus is hostile
                self.failures += 1
                # Drop the connection and rebuild it next cycle. A converter
                # that has been power-cycled will not recover on a stale
                # socket.
                try:
                    if client is not None:
                        client.close()
                except Exception:
                    pass
                client = None
                self.sink.device_state(self.spec.label, False, str(exc))

            self.cycles += 1
            elapsed = time.monotonic() - started
            self._stopping.wait(max(0.0, self.spec.interval_s - elapsed))

        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _one_pass(self, client) -> None:
        batch: list[Reading] = []
        for device in self.spec.devices:
            if self._stopping.is_set():
                break

            # A device in backoff is not touched at all - no request, no
            # timeout, no bus time. This is the difference between one dead
            # device costing two seconds every pass and costing two seconds
            # every sixth pass.
            remaining = self._backoff.get(device.code, 0)
            if remaining > 0:
                self._backoff[device.code] = remaining - 1
                self.reads_skipped += 1
                continue

            ok = self._read_device(client, device, batch)
            if ok:
                self._backoff.pop(device.code, None)
            else:
                self._backoff[device.code] = self.spec.offline_backoff_cycles
            self._note_state(device.code, ok)
            self._stopping.wait(self.spec.gap_s)

        if batch:
            self.sink.write(batch)

    @property
    def backing_off(self) -> dict[str, int]:
        """Devices currently being skipped, and for how many more passes."""
        return {k: v for k, v in self._backoff.items() if v > 0}

    def _read_device(self, client, device: DeviceSpec,
                     batch: list[Reading]) -> bool:
        answered = False
        for point in device.points:
            registers = client.read(device.unit_id, point.address,
                                    registers_needed(point.kind))
            if registers is None:
                continue
            answered = True
            batch.append(Reading(
                ts=datetime.now(timezone.utc),
                device_code=device.code,
                param=point.param,
                value=decode_point(point, registers),
            ))
        return answered

    def _note_state(self, code: str, online: bool) -> None:
        if self._online.get(code) is online:
            return                      # no change: say nothing
        self._online[code] = online
        self.sink.device_state(code, online,
                               "answering" if online else "no reply")


# ---------------------------------------------------------------------------
# all of them
# ---------------------------------------------------------------------------

class Poller:
    def __init__(self, gateways: list[GatewaySpec], sink: Sink,
                 client_factory) -> None:
        self.workers = [GatewayWorker(g, sink, client_factory) for g in gateways]

    def start(self) -> None:
        for w in self.workers:
            w.start()

    def stop(self) -> None:
        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.join(timeout=5.0)

    @property
    def stats(self) -> dict[str, dict[str, int]]:
        return {w.spec.label: {"cycles": w.cycles,
                               "failures": w.failures,
                               "skipped": w.reads_skipped}
                for w in self.workers}
