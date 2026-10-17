"""
Composing the device list from the database and the register maps.

The device list has lived in config/prototype.toml, which means
commissioning a device has meant editing a file on the plant PC and
restarting a service (debt #3). This is the half that replaces it.

Runs against scripted rows rather than PostgreSQL. The questions are
about composition - which addresses come from where, what happens to a
device nobody has a map for, and whether two reads of unchanged data
compare equal - and none of them need a database. That last one is not a
nicety: equality IS the change detector, so a Plan that differs from
itself would make the poller reload on every single poll.
"""

import logging
from pathlib import Path

import pytest

from jetpace_core import inventory
from jetpace_core.devices import DeviceModel, DeviceType, Point, Register

ROOT = Path(__file__).resolve().parents[3]


IDP = DeviceType(
    model=DeviceModel(
        code="BENCH-1", revision="2026-10", vendor="Jetpace",
        datasheet_ref="x.pdf",
        registers=(
            Register("dp_pv", 40001, "s16", "Pa", "Differential pressure"),
            Register("unit", 40014, "u16", "", "Unit selector"),
        )),
    kind="pressure",
    points=(Point("dp", "dp_pv", "Differential pressure", "Pa", decimals=0),
            Point("dp_unit", "unit", "Unit selector", "", decimals=0)),
    limits="dp",
)
TYPES = {"BENCH-1": IDP}


class FakeCursor:
    """Answers fetchall() from a script, in the order the queries run."""

    def __init__(self, results):
        self.results = list(results)
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchall(self):
        return self.results.pop(0) if self.results else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, gateways, parameters):
        self.cur = FakeCursor([gateways, parameters])

    def cursor(self):
        return self.cur


def gateway_row(**over):
    row = {"id": 1, "label": "sim-01", "host": "127.0.0.1", "port": 5020,
           "timeout_ms": 2000, "gap_ms": 25, "scan_interval_ms": 2000,
           "offline_backoff_cycles": 5}
    return {**row, **over}


def parameter_row(code="DP-001", param="dp", **over):
    row = {"gateway_id": 1, "code": code, "name": "Corridor", "unit_id": 1,
           "model": "BENCH-1", "param": param, "scale": None}
    return {**row, **over}


def plan_of(gateways, parameters, types=TYPES):
    return inventory.plan(FakeConn(gateways, parameters), types)


# --- where each half comes from -------------------------------------------

def test_the_address_comes_from_the_map_and_not_the_database():
    """
    The database says which model a device is and which parameters are
    on; the product's datasheet transcription says where that model's
    registers are. There is no
    address column anywhere in the schema, deliberately - a register map
    somebody can change with an UPDATE statement is a register map that
    can silently stop matching the document it came from.
    """
    plan = plan_of([gateway_row()],
                   [parameter_row(param="dp"), parameter_row(param="dp_unit")])
    device, = plan.gateways[0].devices
    assert [(p.param, p.address, p.kind) for p in device.points] == [
        ("dp", 0, "s16"), ("dp_unit", 13, "u16")]


def test_the_gateways_timings_come_from_its_row_in_milliseconds():
    plan = plan_of([gateway_row(timeout_ms=1500, gap_ms=40,
                                scan_interval_ms=3000,
                                offline_backoff_cycles=7)],
                   [parameter_row()])
    g = plan.gateways[0]
    assert (g.timeout_s, g.gap_s, g.interval_s) == (1.5, 0.04, 3.0)
    assert g.offline_backoff_cycles == 7


def test_a_parameter_the_site_turned_off_is_not_polled():
    """
    device_parameter.enabled is filtered in SQL, so a disabled parameter
    produces no row at all and its point is simply absent.
    """
    plan = plan_of([gateway_row()], [parameter_row(param="dp")])
    device, = plan.gateways[0].devices
    assert [p.param for p in device.points] == ["dp"]


def test_a_site_scale_override_beats_the_catalogues_own():
    plan = plan_of([gateway_row()],
                   [parameter_row(param="dp", scale=9.80665)])
    point, = plan.gateways[0].devices[0].points
    assert point.scale == pytest.approx(9.80665)


def test_no_override_falls_back_to_the_catalogue_and_not_to_one():
    """
    Null in device_parameter.scale is the common case and means "the
    product's own figure", which is 1.0 today only because debt #6 says
    the scaling is unconfirmed. If that figure ever changes, a
    fallback hardcoded to 1.0 here would ignore it.
    """
    catalogue = DeviceType(
        model=IDP.model, kind="pressure", limits="dp",
        points=(Point("dp", "dp_pv", "DP", "Pa", scale=0.1),))
    plan = plan_of([gateway_row()], [parameter_row(param="dp", scale=None)],
                   types={"BENCH-1": catalogue})
    assert plan.gateways[0].devices[0].points[0].scale == 0.1


# --- what cannot be read is named, never dropped --------------------------

def test_a_device_whose_model_has_no_map_is_named_and_not_silently_dropped():
    """
    The worst outcome available: the device is configured, nothing polls
    it, the dashboard shows it offline, and nothing anywhere says why.
    This is a real case - an install serving only `environment` has no map
    for an energy meter, and config/products.toml decides that.
    """
    plan = plan_of([gateway_row()],
                   [parameter_row(code="EM-01", model="EM-6400",
                                  param="kwh_import")])
    assert plan.gateways[0].devices == ()
    assert len(plan.unreadable) == 1
    complaint, = plan.unreadable
    assert "EM-01" in complaint and "EM-6400" in complaint
    assert "products.toml" in complaint


def test_a_device_with_every_parameter_disabled_is_named_too():
    """
    Polling it would walk the bus and store nothing. The row exists -
    device_parameter had a row for a parameter the catalogue does not
    read - so it is not caught by the filter above.
    """
    plan = plan_of([gateway_row()],
                   [parameter_row(param="something_else")])
    assert plan.gateways[0].devices == ()
    assert "every parameter" in plan.unreadable[0]


def test_one_unreadable_device_does_not_cost_the_readable_ones():
    plan = plan_of([gateway_row()],
                   [parameter_row(code="DP-001", param="dp"),
                    parameter_row(code="EM-01", model="EM-6400",
                                  param="kwh_import")])
    assert [d.code for d in plan.gateways[0].devices] == ["DP-001"]
    assert len(plan.unreadable) == 1


# --- a fresh install ------------------------------------------------------

def test_an_empty_database_is_an_empty_plan_and_not_an_error():
    """
    Unlike cli.load_config, which exits when the file describes no
    gateways. A site starts with nothing commissioned and fills up from
    the dashboard; a poller that refused to start until somebody had
    edited a file would defeat the entire feature.
    """
    plan = plan_of([], [])
    assert plan.gateways == () and plan.unreadable == ()
    assert plan.devices == 0 and plan.points == 0


def test_a_gateway_with_no_devices_yet_still_produces_a_worker():
    """
    A converter commissioned before the instruments behind it. The worker
    connects, walks an empty list, and picks the devices up on reload -
    which is better than the gateway appearing only once a device does.
    """
    plan = plan_of([gateway_row()], [])
    assert len(plan.gateways) == 1
    assert plan.gateways[0].devices == ()


# --- equality is the change detector --------------------------------------

def test_two_reads_of_the_same_data_are_equal():
    """
    The poller decides whether to reload by comparing this to what it
    has. A Plan that differs from itself - through a set iteration order,
    a dict, an unsorted list - would reload on every poll, tearing down
    and rebuilding every worker every ten seconds forever.
    """
    rows = ([gateway_row(), gateway_row(id=2, host="10.0.0.9", port=502)],
            [parameter_row(param="dp"), parameter_row(param="dp_unit"),
             parameter_row(code="DP-002", unit_id=2, param="dp")])
    first = plan_of(*rows)
    second = plan_of(*rows)
    assert first == second
    assert first.gateways == second.gateways


def test_devices_are_ordered_by_unit_address_within_a_gateway():
    """
    The order devices are walked on a shared bus. Stable, and the same
    order the sheet and the dashboard show, so a log line can be read
    against either.
    """
    plan = plan_of([gateway_row()],
                   [parameter_row(code="DP-003", unit_id=3, param="dp"),
                    parameter_row(code="DP-001", unit_id=1, param="dp"),
                    parameter_row(code="DP-002", unit_id=2, param="dp")])
    assert [d.unit_id for d in plan.gateways[0].devices] == [1, 2, 3]


def test_the_queries_only_read_live_enabled_things():
    assert "retired_at IS NULL" in inventory.DEVICE_PARAMETERS
    assert "d.enabled" in inventory.DEVICE_PARAMETERS
    assert "dp.enabled" in inventory.DEVICE_PARAMETERS
    assert "g.enabled" in inventory.GATEWAYS


def test_the_device_query_is_ordered_so_the_plan_is_deterministic():
    assert "ORDER BY" in " ".join(inventory.DEVICE_PARAMETERS.split())
    assert "ORDER BY" in " ".join(inventory.GATEWAYS.split())


# --- the summary line an operator reads -----------------------------------

def test_the_summary_says_when_something_is_not_readable():
    plan = plan_of([gateway_row()],
                   [parameter_row(code="EM-01", model="EM-6400", param="k")])
    assert "NOT READABLE" in plan.summary()


def test_the_summary_is_quiet_when_everything_is_fine():
    plan = plan_of([gateway_row()], [parameter_row()])
    assert "NOT READABLE" not in plan.summary()
    assert "1 device(s)" in plan.summary()


# --- the register maps the products bring ---------------------------------

def test_a_product_that_will_not_import_costs_readable_devices_not_the_process(
        caplog):
    """
    Called from the poller, where a missing product must mean fewer
    readable devices rather than a dead acquisition process. The devices
    that cannot be read then show up in Plan.unreadable, which is where
    it becomes visible.
    """
    with caplog.at_level(logging.WARNING):
        assert inventory.load_device_types(["notaproduct"]) == {}
    assert "notaproduct" in caplog.text


def test_two_products_describing_one_model_is_fatal(monkeypatch):
    """
    The same SystemExit web.products.device_types raises, and for the
    same reason: picking the first would decide which datasheet a site
    reads its devices with, depending on how products.toml is ordered.
    """
    import sys
    import types as pytypes

    for slug in ("alpha", "beta"):
        module = pytypes.ModuleType(f"jetpace_{slug}")
        module.DEVICE_TYPES = (IDP,)
        monkeypatch.setitem(sys.modules, f"jetpace_{slug}", module)

    with pytest.raises(SystemExit) as exc:
        inventory.load_device_types(["alpha", "beta"])
    assert "BENCH-1" in str(exc.value)


def test_a_product_with_no_catalogue_is_not_a_problem(monkeypatch):
    """
    Energy's state today: no meter register map in the repository. It
    contributes nothing and says nothing, which is correct - a warning
    every ten seconds about a product that is working as intended is how
    a log stops being read.
    """
    import sys
    import types as pytypes

    module = pytypes.ModuleType("jetpace_silent")
    monkeypatch.setitem(sys.modules, "jetpace_silent", module)
    assert inventory.load_device_types(["silent"]) == {}


# --- the reason the catalogue sits at a package root ----------------------

def test_reading_the_device_list_does_not_load_a_web_framework():
    """
    The poller imports the product package to get a register map. If
    DEVICE_TYPES lived under `.web` - where it was first written - that
    import would pull FastAPI and Jinja2 into the acquisition process to
    answer a question about Modbus, and would invert the dependency
    direction architecture.md section 3 sets out.

    A subprocess, because this process has already imported everything.
    """
    import subprocess
    import sys

    # The self-check is that a PRODUCT PACKAGE was imported, not that it
    # yielded a register map. Products legitimately carry none - every
    # one in this repository does today - and asserting on the map would
    # make this test start passing by not running.
    probe = (
        "import sys;"
        "import jetpace_core.inventory as i, jetpace_core.selection as s;"
        "i.load_device_types(s.KNOWN);"
        "loaded = [m for m in sys.modules if m.startswith('jetpace_') "
        "          and not m.startswith('jetpace_core')];"
        "assert loaded, 'no product package was imported - probe is not "
        "proving anything';"
        "bad = [m for m in ('fastapi', 'jinja2', 'starlette', 'uvicorn') "
        "      if m in sys.modules];"
        "print(','.join(bad))"
    )
    # TIMEOUT, because subprocess.run without one waits for ever. A child
    # that blocks - on an import, on a lock, on a machine under memory
    # pressure - turns this into a test suite that never finishes and
    # never says why, which is a worse failure than the one it checks for.
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=60,
            env={**__import__("os").environ,
                 "PYTHONPATH": __import__("os").pathsep.join(sys.path)},
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the import probe did not finish in 60 s")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"the poller's device list pulled in {result.stdout.strip()}")
