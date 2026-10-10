"""
Register maps, pinned to the manufacturer datasheets.

Every map here is transcribed from a document under `Modbus Mapping/` and
carries the file name it came from. A datasheet revision that moves a
register gets a NEW map with a new revision string, never an edit to an
existing one, so that history stays decodable.

The 4xxxx numbers in the datasheets are the traditional notation for
holding registers. The wire address is the number minus 40001, so
DP_PV at 40001 is read with start address 0. Getting this off by one is
the single most common Modbus mistake, which is why the conversion lives
in one place and is tested.
"""

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
