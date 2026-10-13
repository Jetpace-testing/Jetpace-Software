"""
Register maps for the instruments this product reads, pinned to the
manufacturer datasheets.

Every map here is transcribed from a document under `Modbus Mapping/` and
carries the file name it came from. A datasheet revision that moves a
register gets a NEW map with a new revision string, never an edit to an
existing one, so that history stays decodable.

`Register`, `DeviceModel` and `wire_address` come from
`jetpace_core.devices`: the 40001-offset rule is true of any Modbus
device and is a platform invariant, while which registers an iDP-100 has
is this product's business. The two were one file until the products were
split.
"""

from __future__ import annotations

from jetpace_core.devices import DeviceModel, Register, wire_address

__all__ = ["DeviceModel", "Register", "wire_address",
           "IDP100", "UNIT_MMWC", "UNIT_PA", "BAUD_RATES", "NEVER_WRITE"]

# ---------------------------------------------------------------------------
# Jetpace iDP-100 differential pressure transmitter
#
# Modbus RTU over RS-485, 38400 8N1, slave address 1 by default.
#
# DP_PV is read as SIGNED. A cleanroom pressure cascade can legitimately
# read negative when a door is open or a cascade has reversed, and reading
# it unsigned turns -5 Pa into 65531 Pa - which would both hide a real
# excursion and raise a nonsense alarm.
#
# Open question for commissioning: the datasheet does not state the scaling
# of DP_PV, and 40002/40003 are marked "Reserved" rather than part of the
# value, so this is modelled as a single 16-bit register. Confirm against a
# real device before this reaches a report. See the note in docs.
# ---------------------------------------------------------------------------

IDP100 = DeviceModel(
    code="iDP-100",
    revision="2026-10",
    vendor="Jetpace",
    datasheet_ref="Modbus Mapping/Differential Pressure Transmitter Modbus Mapping.pdf",
    registers=(
        Register("dp_pv", 40001, "s16", "Pa", "Differential pressure"),
        Register("unit", 40014, "u16", "", "0 = mmWC, 1 = Pa", writable=True),
        Register("range_low", 40015, "s16", "", "Lower range", writable=True),
        Register("range_high", 40018, "s16", "", "Higher range"),
        Register("slave_address", 40021, "u16", "", "Slave address", writable=True),
        Register("baud_rate", 40022, "u16", "", "0=9600 1=19200 2=38400", writable=True),
    ),
)

UNIT_MMWC = 0
UNIT_PA = 1

BAUD_RATES = {0: 9600, 1: 19200, 2: 38400}

# Registers that must never be written by this software, whatever a caller
# asks for. Writing any of these to a commissioned device loses contact with
# it, and recovering means a site visit with a laptop and a screwdriver.
NEVER_WRITE = frozenset({40021, 40022, 40031, 40032, 40034})
