"""
What this product offers the commissioning form, and that it agrees with
the file it is about to replace.

The points to poll have been written down twice since the prototype: once
as a register map in `registers.py`, pinned to the datasheet, and once as
`[model.iDP-100.point]` in `config/prototype.toml`, where the poller reads
them. Two copies of an address is how a datasheet correction reaches half
the system.

`DEVICE_TYPES` derives the addresses from the map, which makes it the one
source - but the TOML is what the live poller is reading right now, so the
test that matters is that the two still say the same thing. It is a
TEMPORARY test by design: it fails the day the device list stops coming
from the file, and that is the day to delete it.

Pure: no socket, no database, no clock.
"""

import tomllib
from pathlib import Path

import pytest

from jetpace_core.web.products import device_types as collect
from jetpace_environment import registers
from jetpace_environment.web import DEVICE_TYPES, product

ROOT = Path(__file__).resolve().parents[3]
PROTOTYPE = ROOT / "config" / "prototype.toml"

IDP100 = DEVICE_TYPES[0]


# --- the one source ------------------------------------------------------

def test_the_type_is_built_on_the_pinned_map_and_not_a_copy_of_it():
    """
    Identity, not equality. A type holding its own transcription of the
    datasheet would pass every other test here and still be a second
    copy of the addresses.
    """
    assert IDP100.model is registers.IDP100


def test_the_addresses_come_out_where_the_datasheet_puts_them():
    addresses = {p.param: IDP100.register_for(p).address
                 for p in IDP100.points}
    assert addresses == {"dp": 0, "dp_unit": 13}


def test_the_pressure_value_is_read_signed():
    """
    A cleanroom cascade legitimately reads negative. Unsigned turns -5 Pa
    into 65531 Pa, which both hides a real excursion and raises a
    nonsense alarm - the reason registers.py says so twice.
    """
    assert IDP100.register_for(IDP100.points[0]).kind == "s16"


def test_neither_point_invents_a_scale_factor():
    """
    Debt #6: DP_PV's scaling is unconfirmed against a real instrument. A
    plausible factor written here is exactly how that becomes permanent
    and reaches a batch record. It is one number in one place when the
    cleanroom is on the bench.
    """
    assert [p.scale for p in IDP100.points] == [1.0, 1.0]


def test_the_set_points_land_on_the_pressure_and_not_the_selector():
    """
    The type reads two parameters and only one of them is a measurement.
    LSP and HSP on `dp_unit` would be set points on an enum.
    """
    assert IDP100.limits == "dp"
    assert IDP100.limit_point().unit == "Pa"


def test_the_unit_selector_is_stored_and_not_merely_read():
    """
    Nothing plots it, which makes it look droppable. It says whether the
    instrument is reporting mmWC or Pa, so a transmitter whose unit
    register was changed on site - by a previous integrator, or with the
    vendor's tool - would otherwise report numbers 9.8 times too small
    with nothing anywhere to say why.
    """
    assert "dp_unit" in {p.param for p in IDP100.points}


# --- agreeing with the file the poller is still reading -------------------

def test_the_derived_points_match_the_gateway_sheet_exactly():
    """
    TEMPORARY. Delete this when the device list leaves config/ for the
    database (debt #3) - at which point the TOML has no [model] section
    to disagree with. Until then it is the only thing standing between a
    corrected datasheet and a poller that did not hear about it.
    """
    cfg = tomllib.loads(PROTOTYPE.read_text(encoding="utf-8"))
    in_file = {
        p["param"]: (p["address"], p["kind"], float(p.get("scale", 1.0)))
        for p in cfg["model"][IDP100.code]["point"]
    }
    derived = {
        p.param: (IDP100.register_for(p).address,
                  IDP100.register_for(p).kind,
                  p.scale)
        for p in IDP100.points
    }
    assert derived == in_file


def test_the_file_and_the_map_agree_on_which_model_the_devices_are():
    cfg = tomllib.loads(PROTOTYPE.read_text(encoding="utf-8"))
    models = {d["model"] for g in cfg["gateway"] for d in g.get("device", [])}
    assert models == {IDP100.code}


# --- and that the seam carries it ----------------------------------------

def test_the_product_offers_it_through_the_seam():
    """
    Declaring a type and not putting it on the Product would leave the
    dropdown empty with nothing to say why.
    """
    collected = collect([product()])
    assert collected.keys() == {"iDP-100"}
    slug, device_type = collected["iDP-100"]
    assert slug == "environment"
    assert device_type is IDP100
