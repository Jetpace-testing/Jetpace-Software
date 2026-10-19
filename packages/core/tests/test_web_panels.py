"""
The rest of the design's first screen: the panels, their seams and the
exports.

Same discipline as test_web_composition: a fabricated product rather than
a real one, so core's tests do not depend on a product, and no database -
every query the overview runs is stubbed by name. What is proved here is
the mechanism. That the real products describe their own exceptions
correctly is proved by their own tests.

The test worth reading first is
`test_every_out_of_band_id_exists_on_the_page`. The live partial refreshes
four panels on one request by naming their ids; a renamed id does not
error, it just stops refreshing, and a panel that quietly freezes on a
plant PC is the exact failure this project has been bitten by three
times.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from jetpace_core.web import app as A
from jetpace_core.web import products as P
from jetpace_core.web.app import HERE, create_app

DSN = "postgresql://unused:unused@127.0.0.1:1/unused"
TEMPLATES = HERE / "templates"

NOW = datetime(2026, 10, 14, 10, 32, 45, tzinfo=timezone.utc)


# --- the stub -------------------------------------------------------------

def row(code="D1", name="one", gateway="Bus A", unit_id=1, param="dp",
        param_label="Differential pressure", unit="Pa", decimals=1,
        value=12.0, online=True, age_s=2, quality=0):
    """One row of the live query: a device, a parameter, its newest value."""
    return {"code": code, "name": name, "gateway": gateway,
            "unit_id": unit_id, "param": param, "param_label": param_label,
            "unit": unit, "decimals": decimals, "aggregation":
            "instantaneous", "value": value, "ts": NOW, "quality": quality,
            "online": online, "age_s": age_s}


def stub(monkeypatch, **overrides):
    """
    Every query the overview and its partials run, answered in memory.

    Listed by name on purpose - see the note in test_web_composition's
    own stub. A new panel fails these tests until its query appears
    here, which is the moment to notice the first screen costs another
    query.
    """
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    answers = {
        "summary": {"devices": 3, "online": 2, "online_points": 4,
                    "readings_24h": 4321, "newest": NOW},
        "live": [],
        "distribution": {"devices": 3, "online": 2, "silent": 1,
                         "never_seen": 0, "gateways": 2, "gateways_live": 1},
        "silence": [],
        "suspect": [],
        "device": None,
        "device_points": [],
        "device_shape": {},
        "export_devices": [],
        "export_readings": (),
        # The commissioning form's furniture, which the overview computes
        # so the modal is complete in the page rather than fetched when
        # it opens.
        "tenant_id": 1,
        "suggest_code": "DP-004",
        "device_limits": {},
        # The set points in force, keyed (device code, parameter). The
        # live table compares a reading to them.
        "limits": {},
    }
    answers.update(overrides)

    from jetpace_core.web import queries as q
    monkeypatch.setattr(q, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(q, "summary", lambda conn: answers["summary"])
    monkeypatch.setattr(q, "live", lambda conn: answers["live"])
    monkeypatch.setattr(q, "distribution", lambda conn: answers["distribution"])
    monkeypatch.setattr(q, "silence",
                        lambda conn, limit=12: answers["silence"])
    monkeypatch.setattr(q, "suspect",
                        lambda conn, hours=6, limit=12: answers["suspect"])
    monkeypatch.setattr(q, "device", lambda conn, code: answers["device"])
    monkeypatch.setattr(q, "device_points",
                        lambda conn, code: answers["device_points"])
    monkeypatch.setattr(q, "device_shape",
                        lambda conn, code: answers["device_shape"])
    monkeypatch.setattr(q, "export_devices",
                        lambda conn: answers["export_devices"])
    monkeypatch.setattr(q, "tenant_id", lambda conn: answers["tenant_id"])
    monkeypatch.setattr(q, "device_limits",
                        lambda conn, code: answers["device_limits"])
    monkeypatch.setattr(q, "limits", lambda conn: answers["limits"])

    from jetpace_core.web import writes as w
    monkeypatch.setattr(w, "suggest_code",
                        lambda conn, tenant_id, prefix:
                        answers["suggest_code"])

    class _Rows:
        def __enter__(self): return iter(answers["export_readings"])
        def __exit__(self, *exc): return False

    monkeypatch.setattr(q, "export_readings",
                        lambda conn, hours, limit: _Rows())


def eventful_product(tmp_path: Path, events) -> P.Product:
    """A fabricated product that contributes exceptions and nothing else."""
    templates = tmp_path / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "demo.html").write_text(
        '{% extends "_base.html" %}{% block nav %}demo{% endblock %}'
        "{% block content %}<p>fabricated</p>{% endblock %}",
        encoding="utf-8")

    def routes(shell: P.Shell) -> APIRouter:
        router = APIRouter()

        @router.get("/demo", response_class=HTMLResponse)
        def page(request: Request):
            return shell.templates.TemplateResponse(request, "demo.html", {})

        return router

    return P.Product(slug="demo", label="Demo", templates=templates,
                     routes=routes, events=events)


# --- the out-of-band contract --------------------------------------------

OOB = re.compile(r'id="([^"]+)"[^>]*hx-swap-oob')

# Jinja comments, which these files are full of - the notes explaining
# what the design asked for and why it is not there say the very words
# the assertions below look for.
COMMENT = re.compile(r"\{#.*?#\}", re.S)


def without_comments(path: Path) -> str:
    return COMMENT.sub("", path.read_text(encoding="utf-8"))


def test_every_out_of_band_id_exists_on_the_page():
    """
    THE ONE THAT FAILS SILENTLY IN PRODUCTION.

    /partials/live refreshes four panels on one request: the device table
    is the swap target, and the figures, facility and distribution panels
    come back carrying `hx-swap-oob` plus the id of the element to
    replace. HTMX does not complain about an id that matches nothing - it
    drops the fragment - so renaming a panel on the page would leave it
    rendered once at load and frozen for ever after, with a clean console
    and a green suite.

    So: every id the push template claims must be defined on the page
    that includes it.
    """
    push = (TEMPLATES / "_live_push.html").read_text(encoding="utf-8")
    page = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    claimed = set(OOB.findall(push))
    assert claimed, "the push template no longer refreshes anything"

    for ident in sorted(claimed):
        assert f'id="{ident}"' in page, (
            f"_live_push.html swaps #{ident} out of band, but index.html "
            f"defines no element with that id - the panel would render "
            f"once and then never refresh, in silence.")


def test_the_push_template_targets_the_polled_region_itself():
    """
    The polled element swaps innerHTML, so the response's non-OOB part
    has to be the content of THAT element and nothing else. Including the
    page's own wrapper here would nest a panel inside itself on every
    poll.
    """
    push = (TEMPLATES / "_live_push.html").read_text(encoding="utf-8")
    first = push.split("{% include", 1)[1].split("%}", 1)[0]
    assert "_live.html" in first, (
        "the first include in _live_push.html must be the device panel's "
        "own content, because that is what the swap target replaces")


# --- the exceptions panel ------------------------------------------------

def test_a_products_exceptions_reach_the_panel(tmp_path, monkeypatch):
    seen = {}

    def events(conn):
        seen["conn"] = conn
        return [P.Event(when=NOW, kind="excursion", device="DP-01",
                        where="Cleanroom 1", parameter="Diff. pressure",
                        value="-2.4 Pa", limit="> 0 Pa",
                        detail="7 min at or below zero")]

    app = create_app(DSN, [eventful_product(tmp_path, events)])
    stub(monkeypatch)
    body = TestClient(app).get("/").text

    assert "DP-01" in body
    assert "-2.4 Pa" in body
    assert "Excursion" in body
    assert seen, "the events callable was never given a connection"


def test_one_products_broken_exceptions_do_not_blank_the_panel(tmp_path,
                                                               monkeypatch):
    """
    A product that cannot say whether the plant is in spec is itself a
    fault worth seeing on the first screen - so it becomes a row, not a
    silence and not a 500.
    """
    def explode(conn):
        raise RuntimeError("the rollup is not there")

    app = create_app(DSN, [eventful_product(tmp_path, explode)])
    stub(monkeypatch)
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "Recent Exceptions" in response.text
    assert "unavailable" in response.text
    assert "the rollup is not there" in response.text


def test_the_shells_own_exceptions_need_no_product(monkeypatch):
    """
    Silence and a flagged sample are facts about data arriving, so the
    shell knows them with no product loaded at all.
    """
    stub(monkeypatch,
         silence=[{"code": "MTR-07", "name": "Feeder 7",
                   "gateway": "Bus A", "newest": NOW - timedelta(hours=4),
                   "age_s": 14400}],
         suspect=[{"code": "MTR-02", "name": "Feeder 2", "gateway": "Bus A",
                   "parameter": "Active energy", "unit": "kWh",
                   "decimals": 1, "newest": NOW, "samples": 3,
                   "worst": 2, "value": 1234.5}])

    body = TestClient(create_app(DSN)).get("/").text

    assert "MTR-07" in body and "Silent" in body
    assert "silent for 4 h" in body
    assert "MTR-02" in body and "Suspect" in body
    assert "3 suspect samples" in body


def test_ongoing_exceptions_sort_above_recovered_ones(monkeypatch):
    """
    A site with two hundred silent devices must not push a live pressure
    excursion off the bottom of a panel that shows a dozen rows. The one
    somebody has to act on goes first.
    """
    stub(monkeypatch,
         suspect=[{"code": "OLD", "name": "n", "gateway": "g",
                   "parameter": "p", "unit": "u", "decimals": 1,
                   "newest": NOW, "samples": 1, "worst": 2, "value": 1.0}],
         silence=[{"code": "LIVE", "name": "n", "gateway": "g",
                   "newest": NOW - timedelta(days=9), "age_s": 777600}])

    body = TestClient(create_app(DSN)).get("/").text
    # The silent device is far older but still happening, so it leads.
    assert body.index("LIVE") < body.index("OLD")


def test_an_event_cannot_invent_a_kind():
    """
    Otherwise a product introduces its own severity vocabulary and the
    shell's three badges stop meaning anything - the same rule as Tile's
    colours.
    """
    with pytest.raises(ValueError, match="kind"):
        P.Event(when=NOW, kind="catastrophic", device="x", where="y",
                parameter="z", value="1", limit="2")


def test_the_exceptions_panel_says_it_is_not_an_alarm_log(monkeypatch):
    """
    A panel laid out like an alarm list will be read as one. There is no
    alarm model in this schema, so the page has to say so where somebody
    looking at the rows can see it - not only in a comment in the
    source.
    """
    stub(monkeypatch)
    body = TestClient(create_app(DSN)).get("/").text
    assert "not an alarm log" in body


# --- the facility panel --------------------------------------------------

def test_the_live_table_groups_by_converter_and_heads_it_with_a_place(
        monkeypatch):
    """
    THE GROUPING THAT REPLACED THE FACILITY PANEL. It grouped the same
    devices by the same gateways into a second picture of one fact, and
    cost the table a third of the screen - which the table paid for
    with a horizontal scrollbar.

    A converter serves an area, so its label heads the block and the
    rooms are listed under it.
    """
    stub(monkeypatch, live=[
        row(code="CR-001", name="Testing room", gateway="Block A",
            unit_id=1, param="temp", value=23.5),
        row(code="CR-002", name="Corridor", gateway="Block A",
            unit_id=2, param="temp", value=22.1),
        row(code="CR-010", name="Gowning", gateway="Block B",
            unit_id=1, param="dp", value=-12.5),
    ])

    body = TestClient(create_app(DSN)).get("/").text

    assert "Block A" in body and "Block B" in body
    assert "Testing room" in body and "Corridor" in body
    # Two devices on one converter, one on the other.
    assert "2 devices" in body and "1 device" in body


def test_a_block_shows_only_the_parameters_its_instruments_read(
        monkeypatch):
    """
    A converter carries a run of one kind of instrument. Giving the
    whole table every column every product can report would leave most
    cells empty on every row - which is the sample data's mistake
    rather than a real facility's.
    """
    stub(monkeypatch, live=[
        row(code="CR-001", name="Testing room", gateway="Block A",
            unit_id=1, param="temp", param_label="Temperature",
            unit="°C", value=23.5),
        row(code="CR-001", name="Testing room", gateway="Block A",
            unit_id=1, param="rh", param_label="Relative humidity",
            unit="%RH", value=48.2),
        row(code="DP-001", name="Gowning", gateway="Block B",
            unit_id=1, param="dp", param_label="Differential pressure",
            unit="Pa", value=-12.5),
    ])

    body = TestClient(create_app(DSN)).get("/").text

    def block(place):
        """One group, from its heading to the end of its own table."""
        start = body.index(place)
        return body[start:body.index("</table>", start)]

    a, b_block = block("Block A"), block("Block B")
    assert "Temperature" in a and "Relative humidity" in a
    assert "Differential pressure" not in a
    assert "Differential pressure" in b_block
    assert "Temperature" not in b_block


# --- the distribution donut ----------------------------------------------

def test_the_donut_stops_come_from_the_counts(monkeypatch):
    """
    The ring is drawn from the same query as the list beside it, with the
    stops written into the element - so the two cannot drift. Eight
    devices, five online, two silent: 62.5% then 87.5%.
    """
    stub(monkeypatch, distribution={
        "devices": 8, "online": 5, "silent": 2, "never_seen": 1,
        "gateways": 2, "gateways_live": 2})

    body = TestClient(create_app(DSN)).get("/").text

    assert "--stop-1: 62.500%" in body
    assert "--stop-2: 87.500%" in body
    assert "62.5%" in body and "25.0%" in body and "12.5%" in body


def test_the_donut_is_never_the_only_encoding(monkeypatch):
    """
    A conic gradient says nothing to a screen reader and nothing to a
    colourblind reader, so it is marked presentational and every slice is
    named and counted in the list.
    """
    stub(monkeypatch)
    body = TestClient(create_app(DSN)).get("/").text
    assert 'class="donut" aria-hidden="true"' in body
    for slice_ in ("Online", "Silent", "Never reported"):
        assert f"{slice_}</span>" in body


def test_no_devices_draws_an_empty_state_not_a_whole_ring(monkeypatch):
    """
    Zero of zero online would compute 0% and 0%, which paints the entire
    ring the never-reported colour - a full amber circle on an install
    with nothing commissioned yet.
    """
    stub(monkeypatch, distribution={
        "devices": 0, "online": 0, "silent": 0, "never_seen": 0,
        "gateways": 0, "gateways_live": 0})

    body = TestClient(create_app(DSN)).get("/").text
    assert "No devices commissioned" in body
    assert "--stop-1" not in body


# --- sparklines ----------------------------------------------------------

def test_a_flat_series_draws_down_the_middle():
    """
    Pinning a flat line to the floor would read as "it fell to zero",
    which is the one thing a flat line definitely does not mean.
    """
    points = A.sparkline([7.0, 7.0, 7.0])
    ys = {p.split(",")[1] for p in points.split()}
    assert ys == {f"{A.SPARK_H / 2:.2f}"}


def test_a_high_reading_is_at_the_top():
    """SVG y grows downward, so this is the easy sign error to make."""
    points = [p.split(",") for p in A.sparkline([1.0, 9.0]).split()]
    assert float(points[0][1]) > float(points[1][1])


def test_nothing_to_draw_draws_nothing():
    assert A.sparkline([]) == ""


# --- the exports ---------------------------------------------------------

def test_the_reports_panel_offers_only_routes_that_exist(monkeypatch):
    """
    The design lists three finished PDFs. There is no report generator
    here, so every row must be an export that actually answers - a
    download button that 404s on a demo is worse than a shorter panel.
    """
    stub(monkeypatch)
    client = TestClient(create_app(DSN))
    body = client.get("/").text

    hrefs = re.findall(r'<a class="download" href="([^"]+)"', body)
    assert hrefs, "the reports panel offers nothing"
    for href in hrefs:
        assert client.get(href).status_code == 200, href


def test_an_export_window_is_clamped(monkeypatch):
    """
    An unbounded window is a way to take the dashboard down from a URL:
    a day of a 400-meter site is millions of rows.
    """
    asked = {}

    def capture(conn, hours, limit):
        asked["hours"], asked["limit"] = hours, limit

        class _Cur:
            def __enter__(self): return iter(())
            def __exit__(self, *exc): return False
        return _Cur()

    stub(monkeypatch)
    monkeypatch.setattr("jetpace_core.web.queries.export_readings", capture)

    client = TestClient(create_app(DSN))
    client.get("/export/readings.csv?hours=999999").text

    assert asked["hours"] == A.EXPORT_MAX_HOURS
    assert asked["limit"] == A.EXPORT_MAX_ROWS


def test_an_export_says_how_many_rows_it_gave(monkeypatch):
    """
    A truncated export that looks complete is worse than one that
    refuses, so the count is in the file.
    """
    rows = [{"ts": NOW, "device": "D1", "device_name": "one, with a comma",
             "gateway": "Bus A", "parameter": "dp", "unit": "Pa",
             "value": 12.5, "quality": 0}]

    class _Cur:
        def __enter__(self): return iter(rows)
        def __exit__(self, *exc): return False

    stub(monkeypatch)
    monkeypatch.setattr("jetpace_core.web.queries.export_readings",
                        lambda conn, hours, limit: _Cur())

    body = TestClient(create_app(DSN)).get("/export/readings.csv").text

    assert "# 1 rows" in body
    # And a value containing a comma survives the trip.
    assert '"one, with a comma"' in body


def test_the_device_export_is_a_real_inventory(monkeypatch):
    stub(monkeypatch, export_devices=[{
        "code": "D1", "name": "one", "gateway": "Bus A", "host": "10.0.0.5",
        "port": 502, "unit_id": 3, "model": "BENCH-1", "model_revision": "B",
        "vendor": "Acme", "kind": "temperature", "site": "Plant 1",
        "scan_class": "instantaneous", "commissioned_at": NOW}])

    body = TestClient(create_app(DSN)).get("/export/devices.csv").text

    assert "model_revision" in body
    assert "BENCH-1" in body
    assert NOW.isoformat() in body


# --- the modal -----------------------------------------------------------

def test_the_add_device_modal_offers_no_form_without_a_register_map(
        monkeypatch):
    """
    This test used to assert the modal had NO form at all, because there
    was no write path and a form that collects fields it cannot save is
    the worst thing to put in front of a customer. The form exists now -
    but the reasoning survives for the case that still cannot be served.

    An install whose products carry no register map has nothing the form
    could commission, because the addresses to read come from the map and
    from nowhere else. A shell with no products is exactly that install,
    and this is what it has to do: say so, rather than offer a dropdown
    with no entries and six fields that lead to a rejection.
    """
    stub(monkeypatch)
    body = TestClient(create_app(DSN)).get("/").text

    modal = body.split('id="modal-add-device"', 1)[1]
    modal = modal.split("modal-backdrop", 1)[0]
    assert "<input" not in modal
    # Whitespace-collapsed, because the copy wraps in the template and a
    # test that breaks on a reflowed paragraph teaches people to stop
    # reflowing paragraphs.
    flat = " ".join(modal.split())
    assert "carries a register map" in flat
    assert "no device type to commission" in flat
    assert "products.toml" in flat


def test_the_modal_collects_exactly_what_an_operator_knows(monkeypatch):
    """
    Where the instrument is and what it is called. Nothing about
    registers: there is no field on this form that can put a wrong
    address on a device, because the addresses come from a map pinned to
    a datasheet by a test in the product package.
    """
    from bench import BENCH

    stub(monkeypatch)
    app = create_app(DSN, _one_product())
    body = TestClient(app).get("/").text
    modal = body.split('id="modal-add-device"', 1)[1]

    for field in ("host", "port", "unit_id", "model", "code", "name"):
        assert f'name="{field}"' in modal, field
    # The set point fields are named after the parameter they belong to,
    # so a device held to two bands has two pairs and a rejection can
    # redden the right one.
    for point in BENCH.limit_points():
        assert f'name="low_{point.param}"' in modal, point.param
        assert f'name="high_{point.param}"' in modal, point.param
    for forbidden in ("address", "register", "datasheet_number", "kind",
                      "scale"):
        assert f'name="{forbidden}"' not in modal, forbidden


def test_the_form_carries_no_explanatory_hints(monkeypatch):
    """
    The add-device form used to print a line of help under almost every
    field, plus a note saying which registers the chosen type would be
    read at. All of it has gone: an operator commissioning the fourth of
    thirty instruments is reading the labels, not the prose.

    The thing those lines were PROVING - that the form has no field
    which can set a register address - is still true and still tested,
    by test_the_modal_collects_exactly_what_an_operator_knows above.
    Where a commissioned device's register map came from is shown on the
    device's own page, which is where somebody checking one looks.
    """
    stub(monkeypatch)
    body = TestClient(create_app(DSN, _one_product())).get("/").text
    modal = body.split('id="modal-add-device"', 1)[1].split("</form>", 1)[0]

    assert "40001" not in modal, "the register note is gone"
    for gone in ("The serial-to-TCP converter",
                 "502 in gateway mode",
                 "Set on the instrument",
                 "Permanent. Used in reports",
                 "Leave a side blank"):
        assert gone not in modal, gone

    # The labels, the units and the in-field examples stay: those are
    # what the field IS, not an explanation of it.
    for kept in ('name="host"', 'name="unit_id"', 'name="low_pv"',
                 "Converter address", "Slave", "LSP",
                 'placeholder="192.168.1.50"'):
        assert kept in modal, kept


def test_a_device_page_still_says_where_its_register_map_came_from():
    """
    The fact the form's note was carrying, in the place it belongs: a
    commissioned device shows its model, its revision and the datasheet
    the map is pinned to.
    """
    page = (HERE / "templates" / "device.html").read_text(encoding="utf-8")
    assert "datasheet_ref" in page or "Register map pinned" in page


def test_the_form_does_not_promise_a_device_appears_instantly(monkeypatch):
    """
    A commissioned device sits in the table with no readings until the
    poller's next reload. Without saying so, the honest behaviour looks
    like a fault and somebody deletes the device and tries again.
    """
    from jetpace_core.web.app import DEVICE_RELOAD_SECONDS

    template = (HERE / "templates" / "_commissioned.html").read_text(
        encoding="utf-8")
    assert "reload_seconds" in template
    assert DEVICE_RELOAD_SECONDS >= 1


def _one_product():
    """
    A product with one instrument, so the form has something to offer.

    Core's suite is otherwise product-free and should stay that way. It
    used to load the real environment product here; that product sells
    no instrument any more, and a fabricated one keeps these tests about
    the mechanism. See bench.py.
    """
    from bench import bench_product
    return bench_product()


# --- the trend panel -----------------------------------------------------

def test_the_overview_trend_panel_draws_no_limits():
    """
    The design's legend has "Upper limit" and "Lower limit" as dashed
    thresholds. The limits table exists now (migration 0005) and the
    DEVICE page draws them - but this panel still must not, and the
    reason has changed rather than gone away.

    This chart plots several devices at once. Each has its own set
    points, so "the upper limit" is not one line here; drawing one per
    device turns a readable trend into a dozen dashed horizontals, and
    drawing a single pair would be a specification that belongs to
    whichever device happened to be first.

    What a limit is worth reading against is one instrument, which is
    /devices/<code>.
    """
    markup = without_comments(TEMPLATES / "_trend.html")
    assert "Upper limit" not in markup
    assert "Lower limit" not in markup
    assert "Range in each bucket" in markup


def test_the_device_page_draws_the_set_points_it_was_given():
    """
    The other side of the same point, and the thing §10 listed as
    blocked: "upper and lower limit lines on the trend - same missing
    limits table as the alarm model".
    """
    page = (TEMPLATES / "device.html").read_text(encoding="utf-8")
    assert 'name: "LSP"' in page and 'name: "HSP"' in page
    assert "data-low" in page and "data-high" in page


def test_a_set_point_of_zero_is_not_the_same_as_no_set_point():
    """
    Zero is a real and common set point on a pressure cascade - it is the
    boundary a cascade is specified around - so "absent" and "0" have to
    stay distinguishable all the way to the chart.

    Both of the obvious conversions get this wrong in opposite
    directions: Number("") is 0, which would invent a limit; and
    parseFloat(undefined) is NaN, which ECharts draws as a line at an
    unpredictable place rather than not at all.
    """
    page = (TEMPLATES / "device.html").read_text(encoding="utf-8")
    assert 'raw === undefined || raw === ""' in page
    assert "state.low !== null" in page
    assert "state.high !== null" in page


def test_the_tiles_show_a_set_point_without_judging_it():
    """
    A tile says "LSP -20 · HSP -5 Pa" and does not say "in spec" or turn
    green. A single spot reading against a limit is not a compliance
    statement: an excursion has a duration, an acknowledgement and
    somebody's name on it, and none of those exist (§10 step 6). The
    numbers are a fact; a colour would be a claim.
    """
    markup = without_comments(TEMPLATES / "_device_points.html")
    assert "LSP" in markup and "HSP" in markup
    assert "in spec" not in markup
    assert "tile-limit" in markup
    # `alarm` on a tile is the existing "has gone silent" state, which is
    # about the device answering and not about the reading's value.
    assert "' alarm' if not p.fresh" in markup


def test_one_select_drives_both_the_card_and_the_trend():
    """
    Two controls for one choice is how a screen starts disagreeing with
    itself - the cards showing one instrument while the line shows
    another.
    """
    trend = (TEMPLATES / "_trend.html").read_text(encoding="utf-8")
    page = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    assert 'hx-target="#device-card"' in trend
    assert 'hx-include="#trend-device"' in page, (
        "the device card's poll must carry the selection, or a refresh "
        "snaps it back to the first device")


# --- the figures row -----------------------------------------------------

def test_the_shell_tile_count_matches_the_template():
    """
    SHELL_TILES exists only to work out how to lay the row out, so nothing
    breaks loudly when it is wrong - the row just lays out for the wrong
    number of cards. Pinned to the template that actually draws them.
    """
    markup = without_comments(TEMPLATES / "_kpis.html")
    # The shell's own cards are the literal <article>s; a product's come
    # from the loop, which this deliberately does not count.
    before_loop = markup.split("{% for t in product_tiles", 1)[0]
    assert before_loop.count("<article class=\"kpi-card") == A.SHELL_TILES


@pytest.mark.parametrize("tiles,cap,expected", [
    (9, 5, 5),      # the install carrying both products: 5 and 4
    (9, 4, 3),      # not 4, 4 and 1 - spread over the rows it needs
    (7, 5, 4),      # one product: 4 and 3
    (4, 5, 4),      # no products: one row
    (6, 5, 3),      # 3 and 3, rather than 5 and a lone card
    (1, 5, 1),
    (0, 5, 1),      # never zero: repeat(0, ...) is invalid CSS
])
def test_the_figures_row_is_balanced(tiles, cap, expected):
    assert A.kpi_columns(tiles, cap) == expected


def test_no_row_is_stranded_when_a_layout_exists_that_avoids_it(monkeypatch):
    """
    A lone card under a full row reads as a mistake, and on a wide screen
    it is the first thing the eye lands on.

    Not every count can avoid it - thirteen leaves a remainder of one at
    four, three and two columns alike - so the claim is the honest one:
    whenever SOME column count under the cap would avoid stranding, the
    chosen one does.
    """
    for tiles in range(2, 24):
        for cap in A.KPI_CAPS.values():
            chosen = A.kpi_columns(tiles, cap)
            possible = [c for c in range(2, cap + 1)
                        if tiles <= c or tiles % c != 1]
            if not possible:
                continue
            assert tiles <= chosen or tiles % chosen != 1, (
                f"{tiles} tiles at {chosen} columns strands one card, "
                f"and {possible} would not")


def test_the_grid_carries_a_count_for_every_breakpoint():
    """
    `repeat()` takes an integer and CSS cannot cap one, and the element
    carries these inline where a media query cannot reach them. So every
    breakpoint the stylesheet reads has to be emitted here.
    """
    style = A.kpi_grid(9)
    css = (A.HERE / "static" / "app.css").read_text(encoding="utf-8")
    for name in A.KPI_CAPS:
        prop = f"--kpi-cols{'-' + name if name else ''}"
        assert f"{prop}:" in style
        assert f"var({prop}," in css, (
            f"{prop} is emitted and the stylesheet never reads it")


# --- the type scale ------------------------------------------------------

def test_no_font_size_bypasses_the_ramp():
    """
    The whole point of the ramp is that it moves from one place. A literal
    px anywhere means the next person to raise the type misses it, and the
    page ends up with two scales.
    """
    import re
    for css in [A.HERE / "static" / "app.css"]:
        text = css.read_text(encoding="utf-8")
        strays = re.findall(r"font-size:\s*[0-9.]+(?:px|pt|em|rem)", text)
        assert not strays, f"{css.name} sets {strays} outside the ramp"


def test_chart_text_reads_the_same_ramp():
    """
    ECharts wants a number, so chart labels could easily drift off the
    scale and sit at 11px while the table beside them moved to 13.
    """
    import re
    base = (TEMPLATES / "_base.html").read_text(encoding="utf-8")
    assert "function typeAt(step)" in base
    for page in TEMPLATES.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        strays = re.findall(r"fontSize:\s*\d+\b", text)
        assert not strays, f"{page.name} sets {strays} outside the ramp"


# --- the brand -----------------------------------------------------------

def test_the_logo_files_the_shell_asks_for_are_the_ones_that_exist():
    """
    A missing logo renders a broken-image icon in the top-left corner of
    every page, which is both the most visible possible failure and one
    no test catches unless it is this one. The frozen build ships
    `static/` whole, so a file that is here is a file that ships.
    """
    import re
    base = (TEMPLATES / "_base.html").read_text(encoding="utf-8")
    asked = set(re.findall(r"/static/(brand/[A-Za-z0-9._-]+)", base))
    assert asked, "the shell no longer references a logo"
    for name in sorted(asked):
        assert (A.HERE / "static" / name).is_file(), (
            f"_base.html asks for {name}, which is not in static/")


def test_both_themes_get_a_logo():
    """
    An <img> is a separate document, so one file cannot recolour its own
    tagline - there has to be one per theme, and the stylesheet has to
    show each of them somewhere. Hiding both is a blank corner.
    """
    css = (A.HERE / "static" / "app.css").read_text(encoding="utf-8")
    for variant in ("on-light", "on-dark"):
        assert f".brand-logo.{variant} {{ display: block; }}" in css or \
               f".brand-logo.{variant}  {{ display: block; }}" in css, \
               f"{variant} is never shown"


def test_the_brand_is_not_described_twice_to_a_screen_reader():
    """
    Both logos are in the markup at all times and only one is displayed.
    Two images with the same alt text is the same name announced twice.
    """
    base = (TEMPLATES / "_base.html").read_text(encoding="utf-8")
    block = base.split('<a class="brand"', 1)[1].split("</a>", 1)[0]
    assert block.count("<img") == 2
    assert 'alt=""' in block and 'aria-hidden="true"' in block


def test_the_logos_are_byte_for_byte_the_pack_they_came_from():
    """
    A brand asset that has been resaved, recoloured or re-traced by
    somebody in a hurry is very hard to spot and very annoying to find on
    a customer's wall. The README records where each came from; this
    records what each IS, so a change has to be deliberate.
    """
    import hashlib
    expected = {
        "jetpace-on-light.svg":
            "476ad3ab6e0e8df4e0c1f1987d22e207b4a85ddbc1e17788b34eb374637f43e6",
        "jetpace-on-dark.svg":
            "f1b139b0bd4b85c126166d5a9e8956bd48043d162e05dab79fd305cdf71471e3",
    }
    brand = A.HERE / "static" / "brand"
    for name, digest in expected.items():
        actual = hashlib.sha256((brand / name).read_bytes()).hexdigest()
        assert actual == digest, (
            f"{name} is not the file the pack supplied. If that is "
            f"deliberate, update this hash and the README beside it.")


def test_the_logo_is_a_vector_and_not_a_picture_of_one():
    """
    An SVG wrapping a base64 PNG is the usual way a "vector" logo turns
    out not to be one - it looks right in review and blurs on a 4K panel.
    """
    for svg in (A.HERE / "static" / "brand").glob("*.svg"):
        text = svg.read_text(encoding="utf-8")
        assert "base64" not in text and "<image" not in text, (
            f"{svg.name} embeds a raster")
        assert "<path" in text


def test_the_columns_are_in_the_order_the_product_declared(monkeypatch):
    """
    `LIVE` orders by parameter key so two reads of unchanged data come
    back identical - which puts `rh` before `temp`, alphabetical and
    not how anybody reads a cleanroom. A device type lists its points in
    the order its product meant them, and the device page already lays
    its tiles out that way.
    """
    from bench import BENCH_PAIR

    stub(monkeypatch, live=[
        # Posted in the query's order: beta sorts before alpha.
        row(code="BP-001", name="Room", gateway="Bench", unit_id=1,
            param="beta", param_label="Beta", unit="v", value=2.0),
        row(code="BP-001", name="Room", gateway="Bench", unit_id=1,
            param="alpha", param_label="Alpha", unit="u", value=1.0),
    ])
    from jetpace_core.web import products as P
    product = P.Product(slug="demo", label="Demo",
                        device_types=(BENCH_PAIR,))
    body = TestClient(create_app(DSN, [product])).get("/").text
    head = body[body.index("Bench"):body.index("</thead>",
                                               body.index("Bench"))]
    assert head.index("Alpha") < head.index("Beta")
