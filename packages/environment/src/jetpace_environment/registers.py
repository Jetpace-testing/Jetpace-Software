"""
The Cleanroom Display's register map, transcribed from its datasheet.

Source: "Cleanroom Modbus Mapping", supplied 19 October 2026. Eighty
holding registers, 40001-40080. Every number below is the datasheet's
own 4xxxx figure; `wire_address` subtracts 40001 in one place and the
subtraction is never done by hand here. A test asserts every line of
this file against that document.

THREE CHANNELS, ONE INSTRUMENT. Everything configurable exists per
channel as NAME[0..2], and the channels are fixed: 0 is temperature,
1 is relative humidity, 2 is differential pressure. The variants in
__init__.py differ only in which of them are fitted.

WHAT `writable` MEANS HERE. It is NOT the datasheet's R/W column - the
instrument accepts writes to most of this map, including the registers
that change its own address and baud rate. It is the dashboard's
whitelist: `modbus_client` will refuse a register whose `writable` is
false, so anything left false is unreachable from a browser by
construction rather than by a check somebody could forget. A device
knocked off the bus from a web page is a site visit with a screwdriver.

Four registers are marked in capitals below as never writable even
though the instrument accepts them, because those are the four that
would do it: the slave address, the baud rate, the parity and the
EEPROM commit.

TYPES. The datasheet carries no type column. Every register here is one
16-bit word; the choice between signed and unsigned is ours, and the
rule applied is: anything that can be negative is signed, and anything
that cannot is signed anyway unless it is a code, a count or a bitfield.
A temperature below zero read as unsigned comes back as 65531, and
nothing downstream would question it.

SCALE IS NOT HERE, DELIBERATELY - it belongs to the `Point`, which is the
product's decision about a register rather than part of the datasheet.
These registers report TENTHS: 235 is 23.5. See __init__.py.

And it is a default rather than a fact, because `Decimal_Point_Set[ch]`
at 40022-40024 is writable per channel: how many decimal places a
reading carries is a property of how each instrument was configured. The
register is readable here so commissioning can check it against the
declared figure and store the truth per device.
"""

from __future__ import annotations

from jetpace_core.devices import DeviceModel, Register

DATASHEET = "Cleanroom Modbus Mapping, supplied 2026-10-19"
REVISION = "2026-10-19"

# The channel each [n] suffix refers to, fixed by the datasheet.
TEMP, RH, DP = 0, 1, 2

# ---------------------------------------------------------------------------
# the process values
#
# 40001-40003 ARE AMBIGUOUS IN THE DATASHEET and that ambiguity is
# recorded rather than resolved. They are printed as TEMP_PV/DP_PV1,
# RH_PV/DP_PV2 and DP_PV2/DP_PV3, with the note "see FDU_EN behavior";
# 40065 HDIU_EN then says "0: single DP/PV; 1: DP/PV1". So in one mode
# these three are temperature, humidity and pressure, and in another
# they are three differential pressures for a filter display unit.
#
# The variants in __init__.py read them as temperature, humidity and
# pressure. An instrument configured the other way would report
# humidity out of a pressure register with nothing on screen to say so,
# which is why `commission_device` records what the probe actually read
# and why HDIU_EN is readable below.
# ---------------------------------------------------------------------------

CLEANROOM_REGISTERS = (
    Register("temp_pv", 40001, "s16", "°C", "Temperature"),
    Register("rh_pv", 40002, "s16", "%RH", "Relative humidity"),
    Register("dp_pv", 40003, "s16", "Pa", "Differential pressure"),

    # Write 1 to acknowledge. Not writable from here: there is no alarm
    # model to acknowledge anything from yet, and a control that
    # silences an alarm needs the lifecycle behind it first.
    Register("alarm_ack", 40004, "u16", "", "Alarm acknowledge"),
    Register("door_status", 40005, "u16", "", "Door status (bitfield)"),

    # The instrument's own clock.
    Register("dec_day", 40006, "u16", "", "Clock: day"),
    Register("dec_month", 40007, "u16", "", "Clock: month"),
    Register("dec_year", 40008, "u16", "", "Clock: year"),
    Register("dec_hour", 40009, "u16", "", "Clock: hour"),
    Register("dec_min", 40010, "u16", "", "Clock: minute"),
    Register("dec_sec", 40011, "u16", "", "Clock: second"),

    # Alarm bits, low and high, per channel.
    Register("al_status_l_temp", 40012, "u16", "", "Alarm bits low, temperature"),
    Register("al_status_h_temp", 40013, "u16", "", "Alarm bits high, temperature"),
    Register("al_status_l_rh", 40014, "u16", "", "Alarm bits low, humidity"),
    Register("al_status_h_rh", 40015, "u16", "", "Alarm bits high, humidity"),
    Register("al_status_l_dp", 40016, "u16", "", "Alarm bits low, pressure"),
    Register("al_status_h_dp", 40017, "u16", "", "Alarm bits high, pressure"),
    Register("rl1", 40018, "u16", "", "Relay/output state"),

    # What each channel's sensor is.
    Register("in_type_temp", 40019, "u16", "", "Input type, temperature"),
    Register("in_type_rh", 40020, "u16", "", "Input type, humidity"),
    Register("in_type_dp", 40021, "u16", "", "Input type, pressure"),

    # HOW MANY DECIMAL PLACES THE READING CARRIES, per channel. Read at
    # commissioning to work out the scale; never written from here,
    # because changing it changes what every stored reading meant.
    Register("decimals_temp", 40022, "u16", "", "Decimal places, temperature"),
    Register("decimals_rh", 40023, "u16", "", "Decimal places, humidity"),
    Register("decimals_dp", 40024, "u16", "", "Decimal places, pressure"),

    # Calibration. Read-only from here: calibration is done with the
    # vendor's tool by whoever is accountable for it, and a dashboard
    # that can shift a sensor's zero can shift every reading after it.
    Register("zero_temp", 40025, "s16", "", "Calibration zero, temperature"),
    Register("zero_rh", 40026, "s16", "", "Calibration zero, humidity"),
    Register("zero_dp", 40027, "s16", "", "Calibration zero, pressure"),
    Register("span_temp", 40028, "s16", "", "Calibration span, temperature"),
    Register("span_rh", 40029, "s16", "", "Calibration span, humidity"),
    Register("span_dp", 40030, "s16", "", "Calibration span, pressure"),

    # SET POINT 1 IS THE LOW ONE and set point 2 is the high one,
    # confirmed by the person who owns the instrument. The datasheet
    # itself does not say, which is why it is written down here.
    Register("sp_low_temp", 40031, "s16", "°C", "Low set point, temperature",
             writable=True),
    Register("sp_low_rh", 40032, "s16", "%RH", "Low set point, humidity",
             writable=True),
    Register("sp_low_dp", 40033, "s16", "Pa", "Low set point, pressure",
             writable=True),
    Register("hys_low_temp", 40034, "s16", "°C",
             "Low set point hysteresis, temperature", writable=True),
    Register("hys_low_rh", 40035, "s16", "%RH",
             "Low set point hysteresis, humidity", writable=True),
    Register("hys_low_dp", 40036, "s16", "Pa",
             "Low set point hysteresis, pressure", writable=True),
    Register("sp_high_temp", 40037, "s16", "°C", "High set point, temperature",
             writable=True),
    Register("sp_high_rh", 40038, "s16", "%RH", "High set point, humidity",
             writable=True),
    Register("sp_high_dp", 40039, "s16", "Pa", "High set point, pressure",
             writable=True),
    Register("hys_high_temp", 40040, "s16", "°C",
             "High set point hysteresis, temperature", writable=True),
    Register("hys_high_rh", 40041, "s16", "%RH",
             "High set point hysteresis, humidity", writable=True),
    Register("hys_high_dp", 40042, "s16", "Pa",
             "High set point hysteresis, pressure", writable=True),

    # Alarm offset: how far from the set point the alarm clears.
    Register("alarm_offset_temp", 40043, "s16", "°C",
             "Alarm offset, temperature", writable=True),
    Register("alarm_offset_rh", 40044, "s16", "%RH",
             "Alarm offset, humidity", writable=True),
    Register("alarm_offset_dp", 40045, "s16", "Pa",
             "Alarm offset, pressure", writable=True),

    # Sensor offset: the field correction against a reference.
    Register("sensor_offset_temp", 40046, "s16", "°C",
             "Sensor offset, temperature", writable=True),
    Register("sensor_offset_rh", 40047, "s16", "%RH",
             "Sensor offset, humidity", writable=True),
    Register("sensor_offset_dp", 40048, "s16", "Pa",
             "Sensor offset, pressure", writable=True),

    # Which unit each channel reports in. Read, never written: an
    # instrument whose unit changed under a stored history would make
    # every earlier reading mean something else.
    Register("unit_temp", 40049, "u16", "", "Unit code, temperature"),
    Register("unit_rh", 40050, "u16", "", "Unit code, humidity"),
    Register("unit_dp", 40051, "u16", "", "Unit code, pressure"),

    # The analogue retransmission output, not the Modbus reading.
    Register("op_zero_temp", 40052, "s16", "", "Output zero, temperature"),
    Register("op_zero_rh", 40053, "s16", "", "Output zero, humidity"),
    Register("op_zero_dp", 40054, "s16", "", "Output zero, pressure"),
    Register("op_span_temp", 40055, "s16", "", "Output span, temperature"),
    Register("op_span_rh", 40056, "s16", "", "Output span, humidity"),
    Register("op_span_dp", 40057, "s16", "", "Output span, pressure"),

    Register("update_temp", 40058, "u16", "", "Update rate, temperature"),
    Register("update_rh", 40059, "u16", "", "Update rate, humidity"),
    Register("update_dp", 40060, "u16", "", "Update rate, pressure"),

    # ----- THE FOUR THAT ARE NEVER WRITABLE FROM HERE -----
    #
    # The instrument accepts writes to all of these. Writing the first
    # three loses contact with the device and recovery is a site visit
    # with a laptop; the fourth commits whatever is in volatile memory,
    # which is not a thing to do as a side effect.
    Register("slave_address", 40061, "u16", "", "Slave address (1-247)"),
    Register("baud_rate", 40062, "u16", "", "Baud rate (0=9600 1=19200 2=38400)"),
    Register("parity", 40063, "u16", "", "Parity (0=none 1=even 2=odd)"),
    Register("save_en", 40080, "u16", "", "Commit to EEPROM (write 1221)"),
    # ------------------------------------------------------

    Register("scan", 40064, "u16", "", "Scan/averaging parameter"),

    # WHAT 40001-40003 ACTUALLY MEAN. 0 is a single DP/PV, 1 is
    # DP/PV1 - see the note at the top of this file. Read so that a
    # commissioning record can say which mode the instrument was in.
    Register("hdiu_en", 40065, "u16", "", "Channel mode (FDU_EN/HDIU_EN)"),
    Register("buzzer_en", 40066, "u16", "", "Buzzer enable"),
    Register("mode", 40067, "u16", "", "Operating mode"),

    # Input calibration for current and voltage inputs.
    Register("cal_i_low_temp", 40068, "s16", "", "Current low cal, temperature"),
    Register("cal_i_low_rh", 40069, "s16", "", "Current low cal, humidity"),
    Register("cal_i_low_dp", 40070, "s16", "", "Current low cal, pressure"),
    Register("cal_i_high_temp", 40071, "s16", "", "Current high cal, temperature"),
    Register("cal_i_high_rh", 40072, "s16", "", "Current high cal, humidity"),
    Register("cal_i_high_dp", 40073, "s16", "", "Current high cal, pressure"),
    Register("cal_v_low_temp", 40074, "s16", "", "Voltage low cal, temperature"),
    Register("cal_v_low_rh", 40075, "s16", "", "Voltage low cal, humidity"),
    Register("cal_v_low_dp", 40076, "s16", "", "Voltage low cal, pressure"),
    Register("cal_v_high_temp", 40077, "s16", "", "Voltage high cal, temperature"),
    Register("cal_v_high_rh", 40078, "s16", "", "Voltage high cal, humidity"),
    Register("cal_v_high_dp", 40079, "s16", "", "Voltage high cal, pressure"),
)

# The register that says how many decimal places a channel reports in,
# keyed by the parameter that channel becomes. Read at commissioning;
# see the note at the top of this file.
DECIMALS_REGISTER = {
    "temp": "decimals_temp",
    "rh": "decimals_rh",
    "dp": "decimals_dp",
}

# What a confirmed write has to follow to survive a power cycle: 1221
# into SAVE_EN. Without it the instrument holds the new value, answers a
# read-back with it, and forgets it at the next power interruption - so
# a read-back alone would confirm a set point that is not really there.
SAVE_REGISTER = "save_en"
SAVE_VALUE = 1221


def model(code: str) -> DeviceModel:
    """
    One variant's model row, over the one shared register map.

    The three variants are the same instrument with different sensors
    fitted, so they share every address. They are separate models
    because `device_model.code` is what a commissioned device records,
    and "which variant is on the wall" is a thing a site has to be able
    to answer from the database.
    """
    return DeviceModel(code=code, revision=REVISION, vendor="Jetpace",
                       datasheet_ref=DATASHEET,
                       registers=CLEANROOM_REGISTERS)
