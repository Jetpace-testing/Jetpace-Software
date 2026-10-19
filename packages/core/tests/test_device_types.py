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

from jetpace_core.devices import (AGGREGATIONS, KINDS, DeviceModel,
                                  DeviceType, Point, Register,
                                  check_parameters)
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


def test_a_set_point_cannot_be_asked_for_on_something_unread():
    """
    THIS USED TO BE A VALIDATION AND IS NOW STRUCTURAL.

    A type used to name the parameter its set points applied to, which
    could name one it did not read - so `__post_init__` checked for it.
    The flag lives on the point itself now, so there is nothing to
    disagree with: a limited parameter is a parameter, by construction.
    What is left to assert is that the two stay in step.
    """
    t = a_type()
    for point in t.limit_points():
        assert point in t.points


def test_a_type_may_put_set_points_on_more_than_one_parameter():
    """
    A cleanroom transmitter is held to a temperature band AND a humidity
    band. They are revised independently, so they are two set points,
    two rows and two audit entries - not one pair covering a device.
    """
    t = a_type(points=(Point("temp", "pv", "Temperature", "C", limited=True),
                       Point("rh", "selector", "Humidity", "%",
                             limited=True)))
    assert [p.param for p in t.limit_points()] == ["temp", "rh"]


def test_the_set_point_fields_follow_the_order_of_the_readings():
    """
    The device page lays its reading tiles out in point order. A form
    whose fields come in a different order from the page they describe
    is a form somebody fills in wrongly once.
    """
    t = a_type(points=(Point("b", "pv", "B", "u", limited=True),
                       Point("a", "selector", "A", "v", limited=True)))
    assert [p.param for p in t.limit_points()] == ["b", "a"]


def test_a_type_may_have_no_set_points_at_all():
    """
    A meter with no specification is an ordinary case; the form renders
    no set point fieldset rather than two boxes with no unit beside them.
    """
    assert a_type().limit_points() == ()


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


# --- a parameter key is an identifier in five places ----------------------

def test_a_parameter_key_is_safe_everywhere_it_is_used():
    """
    It becomes a CSV column, a query string in /api/trend?param=, a
    data-param attribute, an MQTT topic segment, and part of an HTML form
    field name (`low_temp`). Nothing validated it at all until
    instruments started sharing keys.
    """
    for bad in ("Temp C", "temp/c", "2temp", "TEMP", "temp-c", ""):
        with pytest.raises(ValueError) as exc:
            Point(bad, "pv", "x", "u")
        assert "parameter key" in str(exc.value)

    for good in ("temp", "rh", "kwh_import", "pm2_5"):
        assert Point(good, "pv", "x", "u").param == good


# --- one parameter row per key, for the whole install ---------------------

def _typed(code, **point):
    model = DeviceModel(code=code, revision="r", vendor="v",
                        datasheet_ref="x.pdf",
                        registers=(Register("pv", 40001, "s16", "u", "PV"),))
    return DeviceType(model=model, kind="temperature",
                      points=(Point(register="pv", **point),))


def test_two_instruments_may_share_a_parameter_key_if_they_agree():
    """
    Two instruments measuring temperature should store it in one place.
    That is the point of a shared key, and it is allowed.
    """
    a = _typed("A-1", param="temp", label="Temperature", unit="C", decimals=1)
    b = _typed("B-1", param="temp", label="Temperature", unit="C", decimals=1)
    check_parameters({"A-1": a, "B-1": b})


def test_two_instruments_disagreeing_about_one_parameter_are_fatal():
    """
    THE FAULT THIS EXISTS TO PREVENT, and it is silent without it.

    `parameter` has one row per key for the whole install, and
    commissioning upserts it with DO UPDATE SET label. So the label is
    overwritten site-wide by whichever type was commissioned last, while
    the unit, aggregation and decimals are frozen by whichever used the
    key first and quietly discarded afterwards - a value rendered to the
    wrong precision, consistently, with nothing on screen to say so.
    """
    a = _typed("A-1", param="temp", label="Temperature", unit="C", decimals=1)
    b = _typed("B-1", param="temp", label="Probe temp", unit="C", decimals=0)
    with pytest.raises(SystemExit) as exc:
        check_parameters({"A-1": a, "B-1": b})
    message = str(exc.value)
    assert "temp" in message
    assert "A-1" in message and "B-1" in message
    # It says WHAT they disagree about and what each one declared.
    assert "label" in message and "decimals" in message


def test_the_disagreement_is_caught_by_the_form_and_by_the_poller():
    """
    Two collectors, two processes, one rule. The poller has no browser to
    report into, so it has to refuse the same thing at startup rather
    than poll a device whose parameter means something else.
    """
    import inspect

    from jetpace_core import inventory
    from jetpace_core.web import products as P

    assert "check_parameters" in inspect.getsource(P.device_types)
    assert "check_parameters" in inspect.getsource(
        inventory.load_device_types)
