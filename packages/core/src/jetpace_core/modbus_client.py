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

from .poller import GatewaySpec

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
