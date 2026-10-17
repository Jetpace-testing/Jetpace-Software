"""
What decode.py has to do.

These tests are the specification. They were written before the code, and
they are how you know you are finished. Read each one before you implement
the function it tests - the test says what correct means.
"""

import struct

import pytest

from jetpace_core.decode import decode_float32, decode_s16, decode_u16


# --- helper: what a device would actually put on the wire -------------------

def as_registers(value: float) -> tuple[int, int]:
    """Split a float into the two 16-bit registers a meter would send."""
    return struct.unpack(">HH", struct.pack(">f", value))


# --- decode_u16 ------------------------------------------------------------

@pytest.mark.parametrize(
    "register, expected",
    [
        (0, 0),
        (1, 1),
        (38400 % 65536, 38400 % 65536),
        (32767, 32767),
        (32768, 32768),       # stays positive - this is the whole point
        (65535, 65535),
    ],
)
def test_u16(register, expected):
    assert decode_u16(register) == expected


# --- decode_s16 ------------------------------------------------------------

@pytest.mark.parametrize(
    "register, expected",
    [
        (0, 0),
        (12, 12),            # a healthy cleanroom cascade, +12 Pa
        (32767, 32767),      # largest positive
        (65535, -1),         # two's complement
        (65531, -5),         # a REVERSED cascade, -5 Pa. Not 65531.
        (32768, -32768),     # most negative
    ],
)
def test_s16(register, expected):
    assert decode_s16(register) == expected


def test_s16_is_not_u16():
    """The bug this function exists to prevent, stated as a test."""
    reversed_cascade = 65531          # a transmitter reporting -5
    assert decode_s16(reversed_cascade) == -5
    assert decode_s16(reversed_cascade) < 0, "a reversed cascade must read negative"


# --- decode_float32 --------------------------------------------------------

@pytest.mark.parametrize("value", [0.0, 50.01, 415.2, 1234.56, -12.5, 1e6])
def test_float32_round_trip(value):
    high, low = as_registers(value)
    assert decode_float32(high, low) == pytest.approx(value, rel=1e-6)


def test_float32_known_vector():
    """
    50.01 Hz, the frequency an EM6400 reports at 43015 in Meril's map.
    Hard-coded on purpose: if this ever changes, something is very wrong.
    """
    assert decode_float32(0x4248, 0x0A3D) == pytest.approx(50.01, abs=1e-3)


def test_float32_word_order_matters():
    """
    Swapping the registers gives a different, wrong number. This is why
    word order is a configuration setting and not a guess.
    """
    high, low = as_registers(415.2)
    assert decode_float32(high, low) == pytest.approx(415.2, rel=1e-6)
    assert decode_float32(low, high) != pytest.approx(415.2, rel=1e-6)
