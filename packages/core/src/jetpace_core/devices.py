"""
How a device model is described, for any instrument on any bus.

The scaffolding half of what used to be `registers.py`: that file held
both this and the iDP-100 differential pressure map, and the map has gone
to packages/environment where the product that sells it lives. What stays
here is the part that is true of a Modbus device regardless of what it
measures.

The 4xxxx numbers in the datasheets are the traditional notation for
holding registers. The wire address is the number minus 40001, so a value
documented at 40001 is read with start address 0. Getting this off by one
is the single most common Modbus mistake, which is why the conversion
lives in one place, is tested, and is listed as an invariant in
CLAUDE.md - a platform rule, not one product's.

Imports nothing of ours and nothing that touches a socket, a database or
a clock, so every test over it runs with none of those.
"""

from __future__ import annotations

from dataclasses import dataclass


def wire_address(datasheet_number: int) -> int:
    """40001 -> 0. The address actually sent on the wire."""
    return datasheet_number - 40001


@dataclass(frozen=True)
class Register:
    """One readable value in a device's map."""

    key: str
    datasheet_number: int
    kind: str  # 's16' | 'u16' | 'f32'
    unit: str
    label: str
    writable: bool = False

    @property
    def address(self) -> int:
        return wire_address(self.datasheet_number)


@dataclass(frozen=True)
class DeviceModel:
    """
    A transcribed datasheet.

    Every map built from this carries the file it came from. A datasheet
    revision that moves a register gets a NEW model with a new revision
    string, never an edit to an existing one, so that history stays
    decodable: a reading taken last year has to be interpretable with the
    map that was in force when it was taken.
    """

    code: str
    revision: str
    vendor: str
    datasheet_ref: str
    registers: tuple[Register, ...]

    def by_key(self, key: str) -> Register:
        for r in self.registers:
            if r.key == key:
                return r
        raise KeyError(f"{self.code} has no register {key!r}")
