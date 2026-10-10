"""
Turning raw Modbus registers into numbers.

A Modbus holding register is 16 bits and nothing more. What those bits mean
is a decision the device's datasheet makes, and every function here encodes
one of those decisions.

Two principles these follow, both borrowed from the Meril codebases:

  * Refuse bad input rather than coerce it. A register value outside
    0..65535 did not come off a Modbus wire, so something upstream is
    broken and silently masking it to 16 bits would hide the real fault.

  * Word order is the caller's problem, not this module's. Some devices
    send the two halves of a float the other way round; that is a
    configuration setting, so `decode_float32` is called with the
    arguments swapped rather than given a flag.
"""

import struct

U16_MAX = 0xFFFF
SIGN_BIT = 0x8000


def decode_u16(register: int) -> int:
    """
    An unsigned 16-bit register. Range 0 to 65535.

    Used for things that cannot be negative: a unit selector, a baud rate
    code, a slave address.

    The value is already what we want, so the real work here is asserting
    the contract - which is why the other two functions call this one first
    instead of validating for themselves.
    """
    if not isinstance(register, int) or isinstance(register, bool):
        raise TypeError(f"register must be an int, got {type(register).__name__}")
    if not 0 <= register <= U16_MAX:
        raise ValueError(
            f"not a 16-bit register value: {register} "
            f"(expected 0..{U16_MAX}; nothing off a Modbus wire is outside this)"
        )
    return register


def decode_s16(register: int) -> int:
    """
    A signed 16-bit register, two's complement. Range -32768 to 32767.

    This is the one that matters. A differential pressure of -5 Pa arrives
    on the wire as 65531. Read it as unsigned and a reversed cleanroom
    cascade reports as sixty-five thousand pascals: a real excursion is
    hidden and a nonsense alarm is raised in its place.

    Two's complement says: if the top bit is set, the value is negative,
    and you get it by subtracting 2**16. So 65531 - 65536 = -5.

    The equivalent via struct, if you prefer to see it as a
    reinterpretation of the same bytes rather than as arithmetic:

        struct.unpack(">h", struct.pack(">H", raw))[0]

    Both are correct. The arithmetic version is kept because it shows what
    two's complement actually means.
    """
    raw = decode_u16(register)
    return raw - 0x10000 if raw & SIGN_BIT else raw


def decode_float32(high: int, low: int) -> float:
    """
    A 32-bit IEEE-754 float spread across two consecutive registers,
    `high` first. This is how the EM6400 energy meters report everything.

    The method is to put the two registers back into the four bytes the
    device originally held, then read those bytes as a float. `>` is
    big-endian, which is what Modbus is on the wire; `HH` is two unsigned
    16-bit integers; `f` is a single-precision float.

    Some devices send the registers the other way round - a real
    configuration setting and a real source of field bugs. Call this with
    the arguments swapped for those, which is why there is no flag.
    """
    four_bytes = struct.pack(">HH", decode_u16(high), decode_u16(low))
    return struct.unpack(">f", four_bytes)[0]
