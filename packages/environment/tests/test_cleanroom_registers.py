"""
The Cleanroom Display's register map, against the document it came from.

**THIS IS THE TEST THE INVARIANT MEANS.** CLAUDE.md: "a register map lives
in the product package that sells the instrument, never in core, and is
pinned to its datasheet by a test in that package." This is that test.

Source: "Cleanroom Modbus Mapping", supplied 19 October 2026. Two pages,
eighty holding registers, 40001-40080, three channels: 0 temperature,
1 humidity, 2 differential pressure.

Every number below was read off that document. If a datasheet revision
moves a register, this test fails - and the fix is a NEW DeviceModel
with a new revision, never an edit to this one, so that readings taken
before the change stay decodable with the map that was in force when
they were taken.
"""

import pytest

from jetpace_core.devices import wire_address
from jetpace_environment import DEVICE_TYPES
from jetpace_environment.registers import (CLEANROOM_REGISTERS,
                                           DECIMALS_REGISTER, SAVE_REGISTER,
                                           SAVE_VALUE)

BY_KEY = {r.key: r for r in CLEANROOM_REGISTERS}

# The datasheet, transcribed a second time, independently of the map.
# Deliberately a flat list of (key, number): a test that derived these
# from the thing it is testing would pass whatever the map said.
DATASHEET = [
    ("temp_pv", 40001), ("rh_pv", 40002), ("dp_pv", 40003),
    ("alarm_ack", 40004), ("door_status", 40005),
    ("dec_day", 40006), ("dec_month", 40007), ("dec_year", 40008),
    ("dec_hour", 40009), ("dec_min", 40010), ("dec_sec", 40011),
    ("al_status_l_temp", 40012), ("al_status_h_temp", 40013),
    ("al_status_l_rh", 40014), ("al_status_h_rh", 40015),
    ("al_status_l_dp", 40016), ("al_status_h_dp", 40017),
    ("rl1", 40018),
    ("in_type_temp", 40019), ("in_type_rh", 40020), ("in_type_dp", 40021),
    ("decimals_temp", 40022), ("decimals_rh", 40023), ("decimals_dp", 40024),
    ("zero_temp", 40025), ("zero_rh", 40026), ("zero_dp", 40027),
    ("span_temp", 40028), ("span_rh", 40029), ("span_dp", 40030),
    ("sp_low_temp", 40031), ("sp_low_rh", 40032), ("sp_low_dp", 40033),
    ("hys_low_temp", 40034), ("hys_low_rh", 40035), ("hys_low_dp", 40036),
    ("sp_high_temp", 40037), ("sp_high_rh", 40038), ("sp_high_dp", 40039),
    ("hys_high_temp", 40040), ("hys_high_rh", 40041), ("hys_high_dp", 40042),
    ("alarm_offset_temp", 40043), ("alarm_offset_rh", 40044),
    ("alarm_offset_dp", 40045),
    ("sensor_offset_temp", 40046), ("sensor_offset_rh", 40047),
    ("sensor_offset_dp", 40048),
    ("unit_temp", 40049), ("unit_rh", 40050), ("unit_dp", 40051),
    ("op_zero_temp", 40052), ("op_zero_rh", 40053), ("op_zero_dp", 40054),
    ("op_span_temp", 40055), ("op_span_rh", 40056), ("op_span_dp", 40057),
    ("update_temp", 40058), ("update_rh", 40059), ("update_dp", 40060),
    ("slave_address", 40061), ("baud_rate", 40062), ("parity", 40063),
    ("scan", 40064), ("hdiu_en", 40065), ("buzzer_en", 40066),
    ("mode", 40067),
    ("cal_i_low_temp", 40068), ("cal_i_low_rh", 40069),
    ("cal_i_low_dp", 40070),
    ("cal_i_high_temp", 40071), ("cal_i_high_rh", 40072),
    ("cal_i_high_dp", 40073),
    ("cal_v_low_temp", 40074), ("cal_v_low_rh", 40075),
    ("cal_v_low_dp", 40076),
    ("cal_v_high_temp", 40077), ("cal_v_high_rh", 40078),
    ("cal_v_high_dp", 40079),
    ("save_en", 40080),
]


@pytest.mark.parametrize("key,number", DATASHEET)
def test_every_register_is_at_the_number_the_datasheet_prints(key, number):
    assert key in BY_KEY, f"{key} is missing from the map"
    assert BY_KEY[key].datasheet_number == number


def test_the_map_is_the_whole_datasheet_and_nothing_else():
    """
    Eighty registers, 40001-40080, none invented and none dropped. A
    map with a register the instrument does not have is a read that
    returns whatever happens to be at that address.
    """
    assert len(CLEANROOM_REGISTERS) == len(DATASHEET) == 80
    assert {r.key for r in CLEANROOM_REGISTERS} == {k for k, _ in DATASHEET}
    numbers = sorted(r.datasheet_number for r in CLEANROOM_REGISTERS)
    assert numbers == list(range(40001, 40081))


def test_the_wire_address_is_the_datasheet_number_minus_40001():
    """
    The commonest Modbus mistake there is. It is applied in one place,
    by `wire_address`, and never written out by hand in a map.
    """
    assert BY_KEY["temp_pv"].address == 0
    assert BY_KEY["dp_pv"].address == 2
    assert BY_KEY["save_en"].address == 79
    for r in CLEANROOM_REGISTERS:
        assert r.address == wire_address(r.datasheet_number)


# --- what the dashboard may write -----------------------------------------

NEVER = ("slave_address", "baud_rate", "parity", "save_en")


@pytest.mark.parametrize("key", NEVER)
def test_the_registers_that_would_lose_the_device_are_not_writable(key):
    """
    THE WHOLE SAFETY MECHANISM IN ONE ASSERTION. The instrument accepts
    writes to all four. Writing the first three loses contact with it
    and recovery is a site visit with a laptop and a screwdriver; the
    fourth commits whatever is in volatile memory, which is not
    something to do as a side effect.

    `modbus_client` refuses a register whose `writable` is false, so
    leaving them false makes them unreachable from a browser by
    construction rather than by a check somebody could forget.
    """
    assert BY_KEY[key].writable is False


def test_only_set_points_hysteresis_and_offsets_are_writable():
    """
    The whitelist, stated as a whole rather than one register at a
    time: anything that appears here by accident is a register a web
    page could change on a qualified instrument.
    """
    writable = {r.key for r in CLEANROOM_REGISTERS if r.writable}
    expected = {
        "sp_low_temp", "sp_low_rh", "sp_low_dp",
        "sp_high_temp", "sp_high_rh", "sp_high_dp",
        "hys_low_temp", "hys_low_rh", "hys_low_dp",
        "hys_high_temp", "hys_high_rh", "hys_high_dp",
        "alarm_offset_temp", "alarm_offset_rh", "alarm_offset_dp",
        "sensor_offset_temp", "sensor_offset_rh", "sensor_offset_dp",
    }
    assert writable == expected


def test_calibration_and_unit_codes_are_read_only():
    """
    Calibration is done with the vendor's tool by whoever is
    accountable for it, and a unit code changed under a stored history
    would make every earlier reading mean something else.
    """
    for key in ("zero_temp", "span_temp", "unit_temp", "unit_rh", "unit_dp",
                "cal_i_low_temp", "cal_v_high_dp", "in_type_temp",
                "decimals_temp", "mode", "hdiu_en"):
        assert BY_KEY[key].writable is False, key


# --- what the datasheet does not say, and somebody did --------------------

def test_set_point_one_is_the_low_one():
    """
    The datasheet calls them SP1 and SP2 and never says which is which.
    The person who owns the instrument did, on 19 October 2026, and it
    is recorded in the register names rather than left to whoever reads
    40031 next.
    """
    assert BY_KEY["sp_low_temp"].datasheet_number == 40031   # SP1[0]
    assert BY_KEY["sp_high_temp"].datasheet_number == 40037  # SP2[0]
    assert BY_KEY["sp_low_dp"].datasheet_number == 40033     # SP1[2]
    assert BY_KEY["sp_high_dp"].datasheet_number == 40039    # SP2[2]


def test_every_channel_has_a_decimal_point_register_to_read():
    """
    How many decimal places a reading carries is configurable per
    channel, so it is read at commissioning rather than declared. A
    parameter with no register to read it from would fall back to a
    guess, and the guess is wrong by a factor of ten.
    """
    for param, key in DECIMALS_REGISTER.items():
        assert key in BY_KEY, param
        assert BY_KEY[key].writable is False


def test_a_written_set_point_has_somewhere_to_be_committed():
    """
    1221 into SAVE_EN. Without it the instrument holds the new value
    and answers a read-back with it, then forgets it at the next power
    interruption - so a read-back alone would confirm a set point that
    is not really there.
    """
    assert SAVE_REGISTER in BY_KEY
    assert BY_KEY[SAVE_REGISTER].datasheet_number == 40080
    assert SAVE_VALUE == 1221


# --- the three variants ---------------------------------------------------

VARIANTS = {t.code: t for t in DEVICE_TYPES}


def test_the_product_sells_three_variants_of_one_instrument():
    """
    One register map, three models differing only in which sensors are
    fitted. Separate models because `device_model.code` is what a
    commissioned device records, and "which variant is on the wall" has
    to be answerable from the database.
    """
    assert set(VARIANTS) == {"iCRDN-230-RT", "iCRDN-230-D", "iCRDN-230-DRT"}
    for t in DEVICE_TYPES:
        assert t.family == "Cleanroom Display"
        assert t.model.registers is CLEANROOM_REGISTERS


@pytest.mark.parametrize("code,params", [
    ("iCRDN-230-RT", ["temp", "rh"]),
    ("iCRDN-230-D", ["dp"]),
    ("iCRDN-230-DRT", ["dp", "temp", "rh"]),
])
def test_each_variant_reads_the_channels_it_is_fitted_with(code, params):
    """
    A point for a channel with no sensor would be a device permanently
    half-silent, and a device that answers on some registers and not
    others is exactly the state commissioning now refuses without a
    reason.
    """
    assert [p.param for p in VARIANTS[code].points] == params


def test_every_parameter_carries_a_set_point():
    """
    A cleanroom is qualified on all of them, and each is revised
    independently - so each gets its own pair of fields, its own
    append-only row and its own audit entry.
    """
    for t in DEVICE_TYPES:
        assert t.limit_points() == t.points


def test_the_variants_agree_about_what_each_parameter_is():
    """
    There is one `parameter` row per key for the whole install, so two
    variants reading temperature must agree down to its decimals.
    `devices.check_parameters` enforces this across every loaded
    product; here it is checked within the family, where a copy-paste
    would break it first.
    """
    seen = {}
    for t in DEVICE_TYPES:
        for point in t.points:
            if point.param in seen:
                assert seen[point.param] == point
            seen[point.param] = point
    assert set(seen) == {"temp", "rh", "dp"}


def test_the_readings_are_signed():
    """
    The datasheet has no type column. Temperature goes below zero, a
    pressure cascade goes below zero, and nothing in this map exceeds
    32767 - so signed is strictly safer and identical where the value
    is positive. Read as unsigned, a room at -5 would report 65531.
    """
    for key in ("temp_pv", "rh_pv", "dp_pv"):
        assert BY_KEY[key].kind == "s16"
    for key in ("sp_low_dp", "sp_high_temp", "sensor_offset_temp"):
        assert BY_KEY[key].kind == "s16", key


def test_a_cleanroom_reading_is_critical_and_not_a_trend():
    """A room is qualified on these. That is the product's call."""
    for t in DEVICE_TYPES:
        assert t.scan_class == "critical"


def test_the_map_says_where_it_came_from():
    """
    Every commissioned device records this string, so a year later the
    question "which document was this read against" has an answer that
    does not depend on the PDF still being on somebody's disk.
    """
    for t in DEVICE_TYPES:
        assert "Cleanroom Modbus Mapping" in t.model.datasheet_ref
        assert t.model.revision == "2026-10-19"
