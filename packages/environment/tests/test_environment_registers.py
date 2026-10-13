"""
The iDP-100 map, pinned to its datasheet.

This file had no test at all while it lived in core, which for
NEVER_WRITE is the cheapest gap in the repo to close: those five
addresses are cited as an invariant in CLAUDE.md and in architecture.md
§7, and writing any of them to a commissioned device costs a site visit
with a laptop and a screwdriver.

Pure: no socket, no database, no clock.
"""

import pytest

from jetpace_core.devices import wire_address
from jetpace_environment import registers


# --- the invariant --------------------------------------------------------

def test_the_never_write_set_is_exactly_the_five_addresses():
    """
    Pinned by value, not by length. Slave address, baud rate, save,
    auto-zero. A sixth address appearing here is a decision somebody
    should have to make deliberately; one disappearing is a device this
    software can brick.
    """
    assert registers.NEVER_WRITE == frozenset(
        {40021, 40022, 40031, 40032, 40034})


def test_every_never_write_address_that_is_mapped_is_marked_writable():
    """
    Not a contradiction - it is the point. The datasheet says these
    registers CAN be written; NEVER_WRITE says this software will not.
    If a register in the map were silently read-only we would be relying
    on the wrong guarantee.
    """
    for register in registers.IDP100.registers:
        if register.datasheet_number in registers.NEVER_WRITE:
            assert register.writable


# --- the map --------------------------------------------------------------

def test_differential_pressure_is_read_signed():
    """
    A reversed cascade is negative. Read unsigned, -5 Pa arrives as
    65531 Pa, which both hides a real excursion and raises a nonsense
    alarm.
    """
    dp = registers.IDP100.by_key("dp_pv")
    assert dp.kind == "s16"
    assert dp.unit == "Pa"


def test_the_datasheet_numbers_convert_to_the_addresses_sent_on_the_wire():
    """40001 -> 0. The single most common Modbus mistake."""
    assert registers.IDP100.by_key("dp_pv").datasheet_number == 40001
    assert registers.IDP100.by_key("dp_pv").address == 0
    assert wire_address(40014) == 13


def test_the_map_names_the_datasheet_it_came_from():
    ref = registers.IDP100.datasheet_ref
    assert ref.endswith(".pdf")
    assert "Modbus Mapping" in ref


def test_the_map_carries_a_revision():
    """
    A datasheet revision that moves a register gets a new map with a new
    revision, never an edit to this one, so a reading taken last year
    stays decodable with the map in force when it was taken.
    """
    assert registers.IDP100.revision


def test_asking_for_a_register_that_does_not_exist_says_so():
    with pytest.raises(KeyError, match="iDP-100"):
        registers.IDP100.by_key("not_a_register")


def test_the_pressure_units_are_distinct():
    assert registers.UNIT_MMWC != registers.UNIT_PA


# --- what came from core --------------------------------------------------

def test_the_generic_scaffolding_comes_from_core():
    """
    Register, DeviceModel and wire_address are platform code - the
    40001 offset is true of any Modbus device and CLAUDE.md lists it as
    an invariant. Only the map itself is this product's.
    """
    assert registers.Register.__module__ == "jetpace_core.devices"
    assert registers.DeviceModel.__module__ == "jetpace_core.devices"
