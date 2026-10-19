"""
A fabricated instrument, for the tests that need a product to have one.

NOT a shortcut, and the reason matters. Core's commissioning form is made
OF what the loaded products declare, so testing it needs at least one
device type in existence. These tests used to load the real environment
product and use its differential pressure transmitter; that instrument
was removed on 17 October 2026 and no product in this repository sells
one today.

Fabricating it here turns out to be the better arrangement anyway. Core
is forbidden to know what any product sells - test_core_names_no_product
scans its source to prove it - and these are CORE's tests. What they
check is the mechanism: that the form offers whatever types are loaded,
reads them at the addresses the map gives, and refuses a type no loaded
product carries. That a particular instrument's registers match a
particular datasheet is a different claim, and it belongs to the package
that sells the instrument.

Deliberately generic: "process value" and "mode", not temperature or
pressure. A bench instrument that sounded like a real product's would
invite somebody to assert a product's behaviour from core's suite.
"""

from jetpace_core import devices

BENCH = devices.DeviceType(
    model=devices.DeviceModel(
        code="BENCH-1", revision="2026-10", vendor="Bench",
        datasheet_ref="fabricated for tests",
        registers=(
            devices.Register(key="pv", datasheet_number=40001, kind="s16",
                             unit="u", label="Process value"),
            devices.Register(key="mode", datasheet_number=40010, kind="u16",
                             unit="", label="Mode"),
        )),
    kind="temperature",
    label="Bench reference instrument",
    points=(
        # decimals=0 deliberately: a commissioning screen showing "12.40"
        # implies a precision the instrument does not have, and one test
        # turns on exactly that.
        devices.Point("pv", "pv", "Process value", "u",
                      aggregation="instantaneous", decimals=0, limited=True),
        devices.Point("mode", "mode", "Mode selector", "",
                      aggregation="instantaneous", decimals=0),
    ),
    code_prefix="BN",
    scan_class="critical",
)


def bench_product():
    """One fabricated product carrying one fabricated instrument."""
    from jetpace_core.web import products as P

    return [P.Product(slug="demo", label="Demo", device_types=(BENCH,))]


# A second fabricated instrument, with TWO parameters carrying set points.
#
# The cleanroom case, in the abstract: one slave reporting two quantities,
# each held to its own band. It exists so core can test a form with a pair
# of set point fields per parameter without core knowing what a cleanroom
# is - and so the type dropdown has two entries, which is the only way to
# test that choosing one brings its own fields.
BENCH_PAIR = devices.DeviceType(
    model=devices.DeviceModel(
        code="BENCH-2", revision="2026-10", vendor="Bench",
        datasheet_ref="fabricated for tests",
        registers=(
            devices.Register(key="first", datasheet_number=40001, kind="s16",
                             unit="u", label="First value"),
            devices.Register(key="second", datasheet_number=40002, kind="u16",
                             unit="v", label="Second value"),
        )),
    kind="temperature",
    label="Bench two-parameter instrument",
    points=(
        devices.Point("alpha", "first", "Alpha", "u",
                      aggregation="instantaneous", decimals=1, limited=True),
        devices.Point("beta", "second", "Beta", "v",
                      aggregation="instantaneous", decimals=0, limited=True),
    ),
    code_prefix="BP",
    default_port=1502,
    scan_class="instantaneous",
)


def bench_pair_product():
    """
    Both fabricated instruments, the two-parameter one first.

    First because the form shows the FIRST entry's fields until a type is
    chosen, and the case worth having on screen by default is the one
    with a pair of set points per parameter.
    """
    from jetpace_core.web import products as P

    return [P.Product(slug="demo", label="Demo",
                      device_types=(BENCH_PAIR, BENCH))]
