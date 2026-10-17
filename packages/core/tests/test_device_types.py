"""
What a product may say about an instrument it sells.

A `DeviceType` becomes an entry in the commissioning form's dropdown, so
every mistake a product can make in one has to surface when the
application starts rather than when a customer submits the form. These
tests are the list of mistakes.

Uses a fabricated model rather than a product's own, so core's suite
stays independent of either product - the real maps are checked against
their own datasheets in the product packages.
"""

from pathlib import Path

import pytest

from jetpace_core.devices import (AGGREGATIONS, KINDS, DeviceModel, DeviceType,
                                  Point, Register)
from jetpace_core.web import products as P

ROOT = Path(__file__).resolve().parents[3]
MIGRATION_0001 = ROOT / "migrations" / "0001_init.sql"


MODEL = DeviceModel(
    code="FAKE-1", revision="2026-01", vendor="Nobody",
    datasheet_ref="nowhere.pdf",
    registers=(
        Register("pv", 40001, "s16", "Pa", "Process value"),
        Register("selector", 40014, "u16", "", "Unit selector"),
        Register("total", 40020, "f32", "kWh", "Running total"),
    ),
)


def a_type(**kwargs) -> DeviceType:
    base = dict(
        model=MODEL, kind="pressure",
        points=(Point("pv", "pv", "Process value", "Pa"),),
    )
    return DeviceType(**{**base, **kwargs})


# --- the address comes from the datasheet, once ---------------------------

def test_a_point_names_a_register_and_the_address_follows():
    """
    The whole reason `Point.register` is a key and not an address: the
    40001 offset is applied in one place, by the transcription, so a point
    cannot disagree with the datasheet it came from.
    """
    t = a_type(points=(Point("dp", "pv", "Differential pressure", "Pa"),))
    register = t.register_for(t.points[0])
    assert register.datasheet_number == 40001
    assert register.address == 0
    assert register.kind == "s16"


def test_a_point_naming_a_register_the_model_does_not_have_is_fatal():
    """
    Fails at import, which means at application start, which means a typo
    in a product never reaches the dropdown.
    """
    with pytest.raises(KeyError) as exc:
        a_type(points=(Point("dp", "pv_typo", "Differential pressure", "Pa"),))
    assert "FAKE-1" in str(exc.value)


# --- what a type may not be ----------------------------------------------

def test_a_type_that_reads_nothing_is_refused():
    """
    It would commission a device that is polled forever and stores no
    value - visible only as a device that is permanently offline.
    """
    with pytest.raises(ValueError) as exc:
        a_type(points=())
    assert "reads nothing" in str(exc.value)


def test_two_registers_cannot_map_to_one_parameter():
    """
    reading's primary key is (device_id, param, ts). Two points sharing a
    parameter would race within a single pass and one would silently
    overwrite the other - ON CONFLICT DO NOTHING means the first write of
    the pair wins, which is not a decision anybody made.
    """
    with pytest.raises(ValueError) as exc:
        a_type(points=(Point("dp", "pv", "A", "Pa"),
                       Point("dp", "selector", "B", "")))
    assert "same parameter" in str(exc.value)


def test_limits_must_be_on_a_parameter_the_type_actually_reads():
    """
    Otherwise the form collects LSP and HSP for something that will never
    produce a reading to compare against them.
    """
    with pytest.raises(ValueError) as exc:
        a_type(limits="humidity")
    assert "humidity" in str(exc.value)


def test_a_type_may_have_no_limits_at_all():
    """
    A meter with no specification is an ordinary case; the form hides the
    set point fields rather than demanding two numbers nobody has.
    """
    assert a_type(limits=None).limit_point() is None


def test_an_unknown_kind_is_refused_before_the_insert_can_fail():
    with pytest.raises(ValueError) as exc:
        a_type(kind="vibration")
    assert "vibration" in str(exc.value)


def test_an_unknown_aggregation_is_refused():
    with pytest.raises(ValueError) as exc:
        Point("x", "pv", "X", "Pa", aggregation="average")
    assert "average" in str(exc.value)


def test_a_port_outside_the_range_is_refused():
    with pytest.raises(ValueError):
        a_type(default_port=70000)


# --- the two vocabularies are the database's ------------------------------

def test_the_kinds_are_exactly_the_migrations_check_constraint():
    """
    KINDS is duplicated from migrations/0001 because it is validated at
    import, long before a connection exists. This is what stops the copy
    drifting: a kind accepted here and rejected by the database would be a
    dropdown entry that fails on submit.
    """
    sql = MIGRATION_0001.read_text(encoding="utf-8")
    block = sql.split("CHECK (kind IN (")[1].split("))")[0]
    in_migration = {w.strip().strip("'") for w in block.replace("\n", " ").split(",")}
    assert in_migration == set(KINDS)


def test_the_aggregations_are_exactly_the_migrations_check_constraint():
    sql = MIGRATION_0001.read_text(encoding="utf-8")
    block = sql.split("CHECK (aggregation IN (")[1].split("))")[0]
    in_migration = {w.strip().strip("'") for w in block.split(",")}
    assert in_migration == set(AGGREGATIONS)


# --- collecting them across products --------------------------------------

def fake_product(tmp_path: Path, slug: str, types=()) -> P.Product:
    templates = tmp_path / slug
    templates.mkdir(parents=True, exist_ok=True)
    return P.Product(slug=slug, label=slug.title(), templates=templates,
                     routes=lambda shell: __import__("fastapi").APIRouter(),
                     device_types=types)


def test_a_product_with_no_types_contributes_none(tmp_path):
    """
    Energy's state today: there is no meter register map in the
    repository, so it sells nothing the form can commission. That has to
    be an empty dropdown section, not a crash.
    """
    assert P.device_types([fake_product(tmp_path, "nothing")]) == {}


def test_types_are_collected_with_the_product_that_sells_them(tmp_path):
    t = a_type()
    collected = P.device_types([fake_product(tmp_path, "demo", (t,))])
    assert collected == {"FAKE-1": ("demo", t)}


def test_two_products_describing_one_model_is_fatal_at_startup(tmp_path):
    """
    Not resolved by load order. Two maps for one model code means two
    products disagree about an instrument's registers, and picking the
    first would decide which datasheet a site reads its devices with -
    silently, and differently depending on how products.toml is ordered.
    """
    with pytest.raises(SystemExit) as exc:
        P.device_types([fake_product(tmp_path, "one", (a_type(),)),
                        fake_product(tmp_path, "two", (a_type(),))])
    message = str(exc.value)
    assert "'one'" in message and "'two'" in message and "FAKE-1" in message


# --- what the dropdown shows ----------------------------------------------

def test_the_dropdown_label_names_the_model_and_what_it_is():
    """
    The model code alone is what an engineer recognises and what the
    database stores; the label is what somebody who has not read the
    datasheet needs. The dropdown shows both.
    """
    assert a_type(label="Pressure transmitter").title == \
        "FAKE-1 · Pressure transmitter"


def test_a_type_with_no_label_still_has_something_to_show():
    assert a_type().title == "FAKE-1"
