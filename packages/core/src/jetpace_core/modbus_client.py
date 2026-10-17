"""
The Modbus client the poller uses.

A thin wrapper over pymodbus, for two reasons. It returns None for "no
reply" instead of raising, because silence is the ordinary failure on a
serial bus and not an exceptional one. And it hides the one part of
pymodbus's API that has changed across versions - the keyword naming the
slave address - so a library upgrade touches this file and nothing else.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .poller import GatewaySpec, PointSpec, decode_point, registers_needed

log = logging.getLogger(__name__)


class ModbusTcpReader:
    """One TCP connection to one gateway, serving all the slaves behind it."""

    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        from pymodbus.client import ModbusTcpClient

        # retries=1 means ONE attempt, deliberately. pymodbus defaults to 3,
        # which silently multiplies every timeout by three: a dead device on
        # a 2 s timeout costs six seconds of bus time, not two, and nothing
        # in the configuration says so. The poller already owns the retry
        # policy - it tries again on the next pass, and backs a dead device
        # off for several passes - so retrying here as well is double
        # counting, and it blocks the healthy devices behind it while it
        # happens.
        self._client = ModbusTcpClient(host, port=port, timeout=timeout_s,
                                       retries=1)
        self._slave_kw = self._detect_slave_kwarg()
        self.host = host
        self.port = port

    def _detect_slave_kwarg(self) -> str:
        """
        pymodbus named this `unit`, then `slave`, then `device_id` across
        major versions. Ask the installed one rather than guessing.
        """
        try:
            params = inspect.signature(
                self._client.read_holding_registers).parameters
        except (TypeError, ValueError):
            return "slave"
        for candidate in ("device_id", "slave", "unit"):
            if candidate in params:
                return candidate
        return "slave"

    def read(self, unit_id: int, address: int, count: int) -> list[int] | None:
        """
        Read `count` holding registers starting at wire address `address`.

        Returns None when the device did not answer or returned an
        exception - both of which are normal and are the caller's business
        to interpret, not grounds for an exception.
        """
        if not self._client.connected and not self._client.connect():
            return None
        try:
            result = self._client.read_holding_registers(
                address, count=count, **{self._slave_kw: unit_id})
        except Exception as exc:               # noqa: BLE001
            log.debug("unit %s addr %s: %s", unit_id, address, exc)
            return None

        if result is None or result.isError():
            return None
        registers = list(getattr(result, "registers", []) or [])
        return registers if len(registers) >= count else None

    def close(self) -> None:
        self._client.close()


def tcp_client_factory(spec: GatewaySpec) -> ModbusTcpReader:
    return ModbusTcpReader(spec.host, spec.port, spec.timeout_s)


# ---------------------------------------------------------------------------
# commissioning: is there actually a device there?
#
# The form asks for a converter address, a slave id and a device type, and
# then has to answer "is this thing on the bus" before writing a device
# row. That question is READ-ONLY and stays read-only: Modbus writes are
# disabled at build time rather than by configuration (CLAUDE.md), and
# commissioning is a database act that records what was found - it does
# not configure the instrument. Instruments carry writable registers for
# the slave address and the baud rate, and a device knocked off the bus
# by a commissioning screen is a site visit.
#
# WHY THE TWO FAILURES ARE REPORTED SEPARATELY. `ModbusTcpReader.read`
# returns None for both "the converter is not there" and "the converter is
# there and that slave did not answer", which is correct for the poller -
# it retries either way. For somebody standing in a plant with a laptop
# they are completely different problems:
#
#   not reachable   wrong IP, wrong port, no route, converter unpowered,
#                   firewall. Nothing to do with the instrument.
#   no answer       wrong slave id, A and B swapped, no termination,
#                   wrong baud rate, instrument unpowered. The network is
#                   fine and the bus is not.
#
# Collapsing them into "failed" would send an engineer to check cabling
# when the address is wrong, and vice versa.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeValue:
    param: str
    value: float


@dataclass(frozen=True)
class Probe:
    """What one short conversation with a device found."""

    reachable: bool = False
    answered: bool = False
    values: tuple[ProbeValue, ...] = ()
    detail: str = ""


def probe(host: str, port: int, unit_id: int,
          points: Sequence["PointSpec"], timeout_s: float = 2.0) -> Probe:
    """
    Read one device once, and say what happened in those terms.

    Bounded deliberately. If the first point does not come back there is
    no reason to try the rest - the slave is not answering and each
    further attempt is another full timeout on a request a browser is
    waiting for. A device type with six points would otherwise make a
    wrong slave id cost twelve seconds.

    Opens its own connection and closes it. A second TCP session to a
    converter the poller is already using is what a commissioning
    engineer does with Modbus Poll today, and converters serialise
    requests onto the serial side; the cost is that this probe can land
    inside another request's reply window and come back empty, which
    reads as "no answer" and is retried by the operator pressing the
    button again.
    """
    reader = ModbusTcpReader(host, port, timeout_s)
    try:
        if not reader._client.connect():
            return Probe(detail=f"nothing answered at {host}:{port}. Check "
                                f"the address and port, that the converter "
                                f"has power, and that this machine can "
                                f"reach it.")

        found: list[ProbeValue] = []
        for point in points:
            registers = reader.read(unit_id, point.address,
                                    registers_needed(point.kind))
            if registers is None:
                if found:
                    # Some points answered and this one did not, which is
                    # a map that does not match the instrument rather than
                    # a bus problem. Worth saying, and worth not failing
                    # on - the device is plainly there.
                    return Probe(
                        reachable=True, answered=True, values=tuple(found),
                        detail=f"register {point.address} did not read. The "
                               f"device answered, so check the device type "
                               f"is the right one for this instrument.")
                return Probe(
                    reachable=True,
                    detail=f"{host}:{port} answered, but unit {unit_id} did "
                           f"not. The converter is reachable, so check the "
                           f"slave address, the A/B wiring, the baud rate "
                           f"and that the instrument has power.")
            found.append(ProbeValue(point.param,
                                    decode_point(point, registers)))

        return Probe(reachable=True, answered=True, values=tuple(found))
    except Exception as exc:                            # noqa: BLE001
        # A hostile bus, a name that does not resolve, a socket the
        # operating system refuses. Reported, never raised: this is
        # answering a question on a form.
        log.debug("probe %s:%s unit %s: %s", host, port, unit_id, exc)
        return Probe(detail=f"could not reach {host}:{port}: {exc}")
    finally:
        try:
            reader.close()
        except Exception:                               # noqa: BLE001
            pass
