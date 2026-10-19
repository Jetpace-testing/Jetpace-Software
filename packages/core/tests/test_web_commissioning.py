"""
The commissioning form, end to end through the application.

A TestClient and a scripted database. The Modbus probe is replaced,
because a test that needs an instrument on a bus is a test that does not
run on a build machine - but what is replaced is the FUNCTION, not the
decision, so everything about which addresses are read and what is done
with the answer is the real code.

Three of these pin faults that a green suite did not catch and a live run
did. They are marked where they appear.
"""

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bench import BENCH, bench_product
from jetpace_core.web import products as P
from jetpace_core.web import queries as q
from jetpace_core.web import writes as w
import jetpace_core.web.app as A_MODULE
from jetpace_core.web.app import HERE, create_app

DSN = "postgresql://unused:unused@127.0.0.1:1/unused"
TEMPLATES = HERE / "templates"


# The instrument these tests commission is fabricated; bench.py says why.
def product():
    return bench_product()


class Answered:
    reachable, answered, detail = True, True, ""

    class _V:
        def __init__(self, param, value):
            self.param, self.value = param, value

    values = (_V("pv", 12.4), _V("mode", 1.0))


class Silent:
    reachable, answered = True, False
    values = ()
    detail = "the converter answered, but unit 9 did not."


class Unreachable:
    reachable = answered = False
    values = ()
    detail = "nothing answered at 10.0.0.9:502."


def client_stubs(monkeypatch):
    """Answer every query the overview runs, in memory."""
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(q, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(q, "summary", lambda conn: {
        "devices": 0, "online": 0, "online_points": 0,
        "readings_24h": 0, "newest": None})
    monkeypatch.setattr(q, "live", lambda conn: [])
    monkeypatch.setattr(q, "distribution", lambda conn: {
        "devices": 0, "online": 0, "silent": 0, "never_seen": 0,
        "gateways": 0, "gateways_live": 0})
    monkeypatch.setattr(q, "facility", lambda conn: [])
    monkeypatch.setattr(q, "silence", lambda conn, limit=12: [])
    monkeypatch.setattr(q, "suspect", lambda conn, hours=6, limit=12: [])
    monkeypatch.setattr(q, "device", lambda conn, code: None)
    monkeypatch.setattr(q, "device_points", lambda conn, code: [])
    monkeypatch.setattr(q, "device_shape", lambda conn, code: {})
    monkeypatch.setattr(q, "tenant_id", lambda conn: 1)
    monkeypatch.setattr(q, "device_limits", lambda conn, code: {})
    monkeypatch.setattr(w, "suggest_code",
                        lambda conn, tenant_id, prefix: f"{prefix}-004")


@pytest.fixture
def client(monkeypatch):
    """The overview, with one fabricated instrument in its catalogue."""
    client_stubs(monkeypatch)
    return TestClient(create_app(DSN, product()), raise_server_exceptions=False)


def set_probe(monkeypatch, result):
    from jetpace_core import modbus_client
    monkeypatch.setattr(modbus_client, "probe",
                        lambda *a, **k: result)


def commission(monkeypatch, client, captured=None, **over):
    """POST the form with sensible defaults, capturing the write."""
    def fake_commission(conn, **kwargs):
        if captured is not None:
            captured.update(kwargs)
        return {"id": 1, "code": kwargs["code"], "name": kwargs["name"],
                "unit_id": int(kwargs["unit_id"]), "scan_class": "critical",
                "commissioned_at": None, "gateway_label": "Bench",
                "gateway_created": True}

    monkeypatch.setattr(w, "commission_device", fake_commission)
    body = {"host": "127.0.0.1", "port": "5021", "unit_id": "5",
            "model": "BENCH-1", "code": "DP-010", "name": "Gowning room",
            "low_pv": "-20", "high_pv": "-5"}
    body.update(over)
    return client.post("/partials/commission", data=body)


# --- the probe ------------------------------------------------------------

def test_a_device_that_answers_shows_what_it_is_reading(monkeypatch, client):
    """
    The numbers are the point. "12 Pa" is how somebody knows they are
    looking at the right instrument and not the one in the next room.
    """
    set_probe(monkeypatch, Answered)
    body = client.post("/partials/probe", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-1"}).text
    assert "answered" in body
    assert "Process value" in body
    assert "12" in body


def test_the_reading_is_shown_to_the_decimals_the_product_declared():
    """
    The form rounds to what the POINT declares, not to what arrived. A
    product that reads a value in whole units says decimals=0, and a
    commissioning screen showing "12.40" would imply a precision the
    instrument does not have.
    """
    assert BENCH.points[0].decimals == 0


def test_a_rounded_reading_does_not_show_a_decimal_point(monkeypatch, client):
    set_probe(monkeypatch, Answered)
    body = client.post("/partials/probe", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-1"}).text
    assert "12.4" not in body and "12" in body


def test_the_two_failures_are_told_apart(monkeypatch, client):
    """
    THE REASON THE PROBE REPORTS `reachable` SEPARATELY. For somebody in
    a plant these are different jobs: a converter that is not there is a
    network problem and has nothing to do with the instrument; a
    converter that answers while the slave does not is a bus problem -
    address, A/B wiring, termination, baud, power. Collapsing them into
    "failed" sends an engineer to check cabling when the address is
    wrong.
    """
    set_probe(monkeypatch, Silent)
    silent = client.post("/partials/probe", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "9",
        "model": "BENCH-1"}).text
    assert "The converter answered" in silent
    assert Silent.detail in silent, "the probe's own explanation is shown"

    set_probe(monkeypatch, Unreachable)
    gone = client.post("/partials/probe", data={
        "host": "10.0.0.9", "port": "502", "unit_id": "1",
        "model": "BENCH-1"}).text
    assert "Nothing answered" in gone
    assert Unreachable.detail in gone


def run_real_probe(monkeypatch, connects: bool, answers: bool):
    """
    The real `probe`, with a reader that does what it is told.

    No socket. What is being exercised is the branching and the wording,
    which is where the value of separating the two failures lives.
    """
    from jetpace_core import modbus_client

    class FakeReader:
        def __init__(self, host, port, timeout_s):
            self.host, self.port = host, port
            self._client = self

        def connect(self):
            return connects

        def read(self, unit_id, address, count):
            return [12] if answers else None

        def close(self):
            pass

    monkeypatch.setattr(modbus_client, "ModbusTcpReader", FakeReader)
    from jetpace_core.poller import PointSpec
    return modbus_client.probe("10.0.0.9", 502, 9,
                               [PointSpec("dp", 0, "s16")])


def test_each_failure_names_the_things_worth_checking_for_it(monkeypatch):
    """
    The two messages have to send somebody to different places. A wrong
    slave address and an unreachable converter both look like silence,
    and this wording is the only thing standing between them.
    """
    gone = run_real_probe(monkeypatch, connects=False, answers=False)
    assert gone.reachable is False and gone.answered is False
    assert "port" in gone.detail and "power" in gone.detail

    silent = run_real_probe(monkeypatch, connects=True, answers=False)
    assert silent.reachable is True and silent.answered is False
    for thing in ("slave address", "wiring", "baud", "power"):
        assert thing in silent.detail, thing
    assert "port" not in silent.detail, (
        "the network is known to be fine here; sending somebody to check "
        "the port is the wrong job")

    found = run_real_probe(monkeypatch, connects=True, answers=True)
    assert found.answered is True and found.values[0].value == 12


def test_a_silent_device_is_offered_an_override_with_a_reason(
        monkeypatch, client):
    """
    Installed and not yet powered is an ordinary case. Refusing outright
    would make the form unusable on a commissioning day; accepting
    silently would lose the fact that nobody ever saw it read.
    """
    set_probe(monkeypatch, Silent)
    body = client.post("/partials/probe", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "9",
        "model": "BENCH-1"}).text
    assert 'name="reason"' in body
    assert "audit" in body


def test_the_probe_reads_exactly_what_the_poller_will_read(monkeypatch,
                                                           client):
    """
    Both build their points from the same register map. A probe that read
    a different address would confirm a device the poller then cannot
    talk to - and it would look like a working commissioning flow.
    """
    seen = {}

    from jetpace_core import modbus_client

    def spy(host, port, unit_id, points, timeout_s=2.0):
        seen["points"] = [(p.param, p.address, p.kind) for p in points]
        return Answered

    monkeypatch.setattr(modbus_client, "probe", spy)
    client.post("/partials/probe", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-1"})

    expected = [(p.param, BENCH.register_for(p).address,
                 BENCH.register_for(p).kind)
                for p in BENCH.points]
    assert seen["points"] == expected


def test_one_probe_at_a_time(client):
    """
    A probe holds a connection to a converter and lands a request on a
    bus the poller is also walking. An operator clicking the button five
    times should queue, not put five sessions on one bus.
    """
    import jetpace_core.web.app as A
    source = Path(A.__file__).read_text(encoding="utf-8")
    assert "probe_lock = threading.Lock()" in source
    assert source.count("with probe_lock:") == 2, (
        "both the probe route and the commission route must hold it")


# --- the write ------------------------------------------------------------

def test_commissioning_reports_what_was_created(monkeypatch, client):
    set_probe(monkeypatch, Answered)
    body = commission(monkeypatch, client).text
    assert "DP-010" in body and "Gowning room" in body
    assert "commissioned" in body


def test_the_form_does_not_promise_the_device_appears_instantly(
        monkeypatch, client):
    """
    It sits in the table with no readings until the poller's next reload.
    Unsaid, the honest behaviour looks like a fault and somebody deletes
    the device and tries again.
    """
    from jetpace_core.web.app import DEVICE_RELOAD_SECONDS
    set_probe(monkeypatch, Answered)
    body = commission(monkeypatch, client).text
    assert f"within {DEVICE_RELOAD_SECONDS} seconds" in body


def test_the_device_is_probed_again_when_it_is_committed(monkeypatch, client):
    """
    NOT TRUSTED FROM THE EARLIER CLICK. Whether it answered is a fact
    that goes into the audit record as the value the instrument was
    accepted on, so it has to be something this process observed at the
    moment it wrote the row - not a hidden field posted by a browser.
    """
    captured = {}
    set_probe(monkeypatch, Answered)
    commission(monkeypatch, client, captured)
    assert captured["answered"] is True
    assert "pv=12" in captured["detail"]


def test_a_silent_device_commits_the_failure_into_the_audit_detail(
        monkeypatch, client):
    captured = {}
    set_probe(monkeypatch, Silent)
    commission(monkeypatch, client, captured, reason="power on Friday")
    assert captured["answered"] is False
    assert captured["reason"] == "power on Friday"
    assert "unit 9" in captured["detail"]


def test_a_rejection_names_the_field_it_is_about(monkeypatch, client):
    """
    So the sentence at the bottom of an eight-field form has something to
    point at. The modal's script reads data-field and marks the input.
    """
    set_probe(monkeypatch, Answered)

    def refuse(conn, **kwargs):
        raise w.WriteRejected("DP-010 is already 'Corridor'.", "code")

    monkeypatch.setattr(w, "commission_device", refuse)
    body = client.post("/partials/commission", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-1", "code": "DP-010", "name": "x"}).text
    assert 'data-field="code"' in body
    assert "already" in body


def test_a_device_type_no_product_sells_is_refused_not_substituted(
        monkeypatch, client):
    """
    The worst outcome this form has available. The register map decides
    which addresses are read, and a device quietly commissioned as
    something else would be read at the wrong addresses forever with
    nothing downstream ever questioning it.
    """
    set_probe(monkeypatch, Answered)
    body = client.post("/partials/commission", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "EM-6400", "code": "EM-001", "name": "x"}).text
    assert "EM-6400" in body
    assert "BENCH-1" in body, "it should say what this install does carry"


# --- faults a green suite missed and a live run found ---------------------

def test_no_template_reads_a_dict_key_that_is_a_dict_method(monkeypatch,
                                                            client):
    """
    FOUND LIVE, as a 500.

    Jinja resolves `thing.values` to the dict's own .values METHOD before
    it looks for a key of that name, so `{% for v in probe.values %}`
    iterated a bound method and raised. Any dict handed to a template has
    keys, values, items, get, copy, update and pop already taken.
    """
    taken = ("keys", "values", "items", "get", "copy", "update", "pop")
    for template in TEMPLATES.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        for name in taken:
            assert f".{name} %}}" not in text, f"{template.name}: .{name}"
            assert f" in probe.{name}" not in text, template.name
            assert f"{{{{ probe.{name} }}}}" not in text, template.name


def test_every_out_of_band_swap_targets_an_id_that_exists(monkeypatch,
                                                          client):
    """
    An hx-swap-oob element whose id is not on the page is dropped in
    SILENCE. The next-code swap is the second place in this application
    that depends on one.
    """
    import re
    body = client.get("/").text
    page_ids = set(re.findall(r'id="([^"]+)"', body))

    for template in TEMPLATES.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        if "hx-swap-oob" not in text:
            continue
        for element in re.findall(r"<[^>]*hx-swap-oob[^>]*>", text):
            found = re.search(r'id="([^"{]+)"', element)
            assert found, f"{template.name}: an OOB swap with no literal id"
            assert found.group(1) in page_ids, (
                f"{template.name}: OOB id {found.group(1)!r} is not on the "
                f"page, so the swap is dropped silently")


def test_the_insert_returns_every_column_the_confirmation_renders():
    """
    FOUND LIVE, as a 500 AFTER the device had been created - the worst
    kind, because the write succeeded and the operator saw a failure.

    DEVICE_INSERT returned only id and commissioned_at while the
    confirmation reads unit_id off the same row.
    """
    returning = w.DEVICE_INSERT.split("RETURNING")[1]
    template = (TEMPLATES / "_commissioned.html").read_text(encoding="utf-8")
    for column in ("code", "name", "unit_id"):
        assert column in returning, column
    assert "device.code" in template


def test_the_live_region_accepts_the_nudge_the_form_sends():
    """
    The form fires a `refresh` event at #live so a commissioned device
    appears at once rather than on the next five-second tick. HTMX
    ignores an event the element's hx-trigger does not list, in silence.
    """
    index = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    trigger = index.split('id="live"', 1)[1].split("hx-trigger=\"", 1)[1]
    trigger = trigger.split('"', 1)[0]
    assert "refresh" in trigger
    assert 'htmx.trigger("#live", "refresh")' in index


def test_the_promised_reload_matches_what_the_service_actually_runs():
    """
    The form tells an operator the device will be picked up "within N
    seconds". N is web/app.py's DEVICE_RELOAD_SECONDS; what actually
    decides it is --reload on the poller child in supervisor.json. The
    two are in different processes and nothing can discover the other, so
    the only thing keeping them honest is this.

    A promise of ten seconds against a poller reloading every sixty is
    how a working feature gets reported as broken.
    """
    import json

    from jetpace_core.web.app import DEVICE_RELOAD_SECONDS

    root = Path(__file__).resolve().parents[3]
    config = root / "supervisor.json"
    if not config.exists():
        pytest.skip("no supervisor.json in this checkout")

    children = json.loads(config.read_text(encoding="utf-8"))["children"]
    poller = next((c for c in children if c["name"] == "poller"), None)
    if poller is None or "--devices" not in poller["args"]:
        pytest.skip("this install still reads its device list from a file")

    args = poller["args"]
    assert args[args.index("--devices") + 1] == "db"
    reload_s = float(args[args.index("--reload") + 1])
    assert reload_s <= DEVICE_RELOAD_SECONDS, (
        f"the form promises {DEVICE_RELOAD_SECONDS}s and the poller "
        f"reloads every {reload_s}s")


def test_the_registration_script_can_change_the_child_list_alone():
    """
    The child list is a file the service reads at start; the registry
    holds the account, the executable and PYTHONPATH. Re-registering to
    change a poller argument is the one operation here that can take the
    live stack down, and it should not be the only way.
    """
    root = Path(__file__).resolve().parents[3]
    script = (root / "scripts" / "register_supervisor_service.py").read_text(
        encoding="utf-8")
    assert '"--config-only"' in script
    assert "Restart-Service" in script


# --- the empty install ----------------------------------------------------
#
# Nothing had ever rendered this application against a database with no
# devices in it. Purging the prototype's synthetic data produced four
# faults in one screen, every one of them cosmetic-looking and every one
# of them a thing a customer would read on their first day.

def test_an_install_with_nothing_commissioned_renders(client):
    """
    The state every site is in for its first hour. A 500 here is the
    first thing a customer sees.
    """
    assert client.get("/").status_code == 200


def test_zero_offline_devices_does_not_claim_everything_is_reporting(client):
    """
    "0 offline - all reporting" is the same words a healthy fleet shows,
    on a site that has no fleet. Three states, not two.
    """
    body = client.get("/").text
    offline = body.split("Offline Devices", 1)[1][:400]
    assert "all reporting" not in offline
    assert "none commissioned" in offline


def test_an_empty_table_does_not_blame_the_operators_filter(client):
    """
    The filter's "No device matches - clear the search or the status
    filter" was revealed whenever nothing matched, including when there
    was nothing to match. It appeared over an empty search box, directly
    beneath the template's own empty state, so the panel gave two
    different explanations at once.
    """
    base = (HERE / "templates" / "_base.html").read_text(encoding="utf-8")
    assert "total === 0 || matched.length !== 0" in base


def test_the_empty_table_does_not_tell_a_customer_to_start_a_simulator(
        client):
    """
    It said "Start the simulator and the poller." - developer copy that
    reached a customer screen the moment a real install was emptied.
    """
    markup = (HERE / "templates" / "_live.html").read_text(encoding="utf-8")
    body = markup.split("{#", 1)[0] + "".join(
        part.split("#}", 1)[-1] for part in markup.split("{#")[1:])
    assert "simulator" not in body.lower()


def test_a_commissioned_device_appears_before_it_has_ever_reported(client):
    """
    THE ONE THAT MATTERED. `LIVE` was an inner join on `reading`, so a
    device that had never answered was not offline, not blank - absent.
    An operator commissions an instrument, the bus is not wired yet, and
    the device they just created is nowhere on the screen with nothing
    to say why. The commissioning confirmation promises the opposite in
    writing.
    """
    from jetpace_core.web import queries as q
    live = " ".join(q.LIVE.split())
    assert "LEFT JOIN device_parameter" in live.replace("LEFT   JOIN",
                                                        "LEFT JOIN")
    assert "LEFT JOIN LATERAL" in live.replace("LEFT   JOIN", "LEFT JOIN")
    # the inner join that caused it
    assert "JOIN   reading" not in q.LIVE


def test_the_table_survives_a_device_with_no_reading_yet(client, monkeypatch):
    """
    And the other half: the query returning a null value must not crash
    the template that formats it. It did.
    """
    from jetpace_core.web import queries as q
    monkeypatch.setattr(q, "live", lambda conn: [{
        "code": "DP-009", "name": "Gowning", "gateway": "Bench",
        "unit_id": 9, "param": "dp", "param_label": "Differential pressure",
        "unit": "Pa", "decimals": 0, "aggregation": "instantaneous",
        "value": None, "ts": None, "online": None, "age_s": None,
    }])
    body = client.get("/").text
    assert "DP-009" in body
    assert "Offline" in body


# --- a set point pair per parameter ---------------------------------------

def pair_client(monkeypatch):
    """A dashboard whose catalogue holds the two-parameter instrument."""
    from bench import bench_pair_product

    client_stubs(monkeypatch)
    return TestClient(create_app(DSN, bench_pair_product()),
                      raise_server_exceptions=False)


def test_each_limited_parameter_gets_its_own_pair_of_fields(monkeypatch):
    """
    A transmitter held to a temperature band AND a humidity band needs
    two pairs. One pair covering a device cannot say which quantity it
    is about, and the unit beside it would be wrong for one of them.
    """
    body = pair_client(monkeypatch).get("/").text
    modal = body.split('id="modal-add-device"', 1)[1]
    for param in ("alpha", "beta"):
        assert f'name="low_{param}"' in modal
        assert f'name="high_{param}"' in modal


def test_a_set_point_field_is_labelled_with_its_own_parameter_and_unit(
        monkeypatch):
    body = pair_client(monkeypatch).get("/").text
    modal = body.split('id="modal-add-device"', 1)[1]
    assert "Alpha LSP" in modal and "Beta LSP" in modal


def test_each_limited_parameter_becomes_its_own_set_point(monkeypatch):
    """
    Two parameters, two `SetPoint`s reaching the write - which becomes
    two append-only rows and two audit entries, because they are revised
    independently.
    """
    captured = {}
    set_probe(monkeypatch, Answered)

    def fake(conn, **kwargs):
        captured.update(kwargs)
        return {"id": 1, "code": kwargs["code"], "name": kwargs["name"],
                "unit_id": 5, "scan_class": "instantaneous",
                "commissioned_at": None, "gateway_label": "B",
                "gateway_created": False}

    monkeypatch.setattr(w, "commission_device", fake)
    pair_client(monkeypatch).post("/partials/commission", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-2", "code": "BP-001", "name": "Room",
        "low_alpha": "18", "high_alpha": "24",
        "low_beta": "30", "high_beta": "60"})

    got = {sp.param: (sp.low, sp.high) for sp in captured["set_points"]}
    assert got == {"alpha": ("18", "24"), "beta": ("30", "60")}


def test_a_rejected_set_point_names_that_parameters_own_field(monkeypatch):
    """
    The modal reddens a field by name. With a pair per parameter, a
    humidity rejection naming "low" would redden the temperature box
    above it.
    """
    set_probe(monkeypatch, Answered)
    body = pair_client(monkeypatch).post("/partials/commission", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-2", "code": "BP-001", "name": "Room",
        "low_beta": "not a number"}).text
    assert 'data-field="low_beta"' in body


def test_a_set_point_for_a_parameter_this_type_does_not_read_is_refused(
        monkeypatch):
    """
    Somebody typed a specification against a quantity this instrument
    does not measure. Dropping it silently loses a specification.
    """
    set_probe(monkeypatch, Answered)
    body = pair_client(monkeypatch).post("/partials/commission", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-2", "code": "BP-001", "name": "Room",
        "low_nonsense": "5"}).text
    assert "does not measure" in body
    assert 'data-field="low_nonsense"' in body


def test_a_blank_field_left_over_from_another_type_is_ignored(monkeypatch):
    """
    The dropdown offered another type a moment ago and the browser still
    has its empty boxes. The operator never saw them, so there is
    nothing to report - unlike a field somebody typed into.
    """
    captured = {}
    set_probe(monkeypatch, Answered)

    def fake(conn, **kwargs):
        captured.update(kwargs)
        return {"id": 1, "code": kwargs["code"], "name": kwargs["name"],
                "unit_id": 5, "scan_class": "instantaneous",
                "commissioned_at": None, "gateway_label": "B",
                "gateway_created": False}

    monkeypatch.setattr(w, "commission_device", fake)
    r = pair_client(monkeypatch).post("/partials/commission", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-2", "code": "BP-001", "name": "Room",
        "low_leftover": "", "high_leftover": ""})
    assert r.status_code == 200
    assert {sp.param for sp in captured["set_points"]} == {"alpha", "beta"}


def test_the_commissioning_route_does_not_run_on_the_event_loop():
    """
    IT READS THE FORM THROUGH A DEPENDENCY SO IT CAN STAY SYNC.

    Making the route `async def` to await request.form() would put a
    blocking Modbus probe - held under the probe lock - and a blocking
    database transaction on the event loop, with every five-second panel
    poll queued behind a commissioning click.
    """
    import inspect

    import jetpace_core.web.app as A

    source = Path(A.__file__).read_text(encoding="utf-8")
    signature = source.split("def partial_commission(", 1)[1][:700]
    assert "async def partial_commission" not in source
    assert "Depends(set_point_fields)" in signature
    assert inspect.iscoroutinefunction(A.set_point_fields)


def test_a_device_that_answered_on_only_some_registers_needs_a_reason(
        monkeypatch):
    """
    FOUND BY ADDING A SECOND PARAMETER. `probe` reports answered=True
    when some registers read and a later one does not - unreachable with
    a one-parameter instrument. With two, a transmitter whose second
    register is silent would commission as answering, with no reason
    asked for and only the first value in the audit record.
    """
    class Half:
        reachable, answered = True, True
        detail = "register 1 did not read."

        class _V:
            def __init__(self, param, value):
                self.param, self.value = param, value

        values = (_V("alpha", 20.0),)

    captured = {}
    set_probe(monkeypatch, Half)

    def fake(conn, **kwargs):
        captured.update(kwargs)
        return {"id": 1, "code": kwargs["code"], "name": kwargs["name"],
                "unit_id": 5, "scan_class": "instantaneous",
                "commissioned_at": None, "gateway_label": "B",
                "gateway_created": False}

    monkeypatch.setattr(w, "commission_device", fake)
    pair_client(monkeypatch).post("/partials/commission", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "BENCH-2", "code": "BP-001", "name": "Room"})
    assert captured["answered"] is False, (
        "a half-answering device must be treated as not answering, so the "
        "write asks for a reason")


# --- instruments this install cannot commission yet ------------------------

def test_a_planned_instrument_is_shown_but_cannot_be_chosen(monkeypatch):
    """
    An engineer looking for their transmitter should learn that it is
    coming, not conclude it is unsupported. Disabled rather than hidden,
    and disabled rather than selectable: there is no register map behind
    it, so the server would only refuse it.
    """
    from jetpace_core import devices
    from jetpace_core.web import products as P

    client_stubs(monkeypatch)
    product = P.Product(slug="demo", label="Demo",
                        device_types=(BENCH,),
                        planned=(devices.Planned("SOON-1", "Not here yet"),))
    body = TestClient(create_app(DSN, [product])).get("/").text
    modal = body.split('id="modal-add-device"', 1)[1]

    assert "SOON-1" in modal and "Not here yet" in modal
    tag = modal.split('value="SOON-1"', 1)[1].split(">", 1)[0]
    assert "disabled" in tag
    assert "awaiting its register map" in modal


def test_a_planned_instrument_cannot_be_commissioned(monkeypatch):
    """
    The dropdown disables it; the server refuses it anyway. A disabled
    option is a browser's courtesy, not a rule.
    """
    from jetpace_core import devices
    from jetpace_core.web import products as P

    client_stubs(monkeypatch)
    product = P.Product(slug="demo", label="Demo",
                        device_types=(BENCH,),
                        planned=(devices.Planned("SOON-1", "Not here yet"),))
    client = TestClient(create_app(DSN, [product]),
                        raise_server_exceptions=False)
    set_probe(monkeypatch, Answered)
    body = client.post("/partials/commission", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "SOON-1", "code": "X-1", "name": "x"}).text
    assert "not a device type this install carries" in body


def test_an_install_with_no_maps_still_says_what_is_coming(monkeypatch):
    """
    THE STATE THE "nothing to commission" MESSAGE IS WEAKEST IN. Told
    only that no device type exists, an engineer cannot tell "your
    instrument is not supported" from "nobody has transcribed its
    datasheet yet" - and the second is the truth on a fresh install.
    """
    from jetpace_core import devices
    from jetpace_core.web import products as P

    client_stubs(monkeypatch)
    product = P.Product(slug="demo", label="Demo",
                        planned=(devices.Planned("SOON-1", "Not here yet"),))
    import re

    body = TestClient(create_app(DSN, [product])).get("/").text
    modal = re.sub(r"\s+", " ", body.split('id="modal-add-device"', 1)[1])

    assert "no device type to commission" in modal
    assert "waiting for their register maps" in modal
    assert "SOON-1" in modal and "Not here yet" in modal
    # Still no form: there is nothing to commission against.
    assert 'id="add-device-form"' not in modal


# --- the form follows the type that was chosen ----------------------------

def test_choosing_a_type_brings_its_own_set_point_fields(monkeypatch):
    """
    Until this route existed, the fields were computed once from the
    FIRST type in the catalogue and nothing updated them. With one type
    that was invisible; with per-parameter set points it would be the
    wrong parameters entirely.
    """
    body = pair_client(monkeypatch).get(
        "/partials/device-type?model=BENCH-1").text
    assert 'name="low_pv"' in body and 'name="high_pv"' in body
    assert "low_alpha" not in body


def test_choosing_a_type_brings_its_own_port_and_code(monkeypatch):
    """
    Both travel as out-of-band swaps, because they sit in other
    fieldsets - and both come from the same request as the fields, so
    the form can never show one type's port beside another's parameters.
    """
    body = pair_client(monkeypatch).get(
        "/partials/device-type?model=BENCH-2").text
    assert 'id="field-port"' in body and 'value="1502"' in body
    assert 'id="field-code"' in body and "BP-004" in body
    assert body.count('hx-swap-oob="true"') == 2


def test_the_type_fields_are_not_duplicated_into_the_form_itself(
        monkeypatch):
    """
    The same template renders in the form and in the response, so the
    two cannot drift. The out-of-band inputs must appear only in the
    response: in the form they would be a second input named `port`,
    and which one the browser posts is not something to leave to chance.
    """
    body = pair_client(monkeypatch).get("/").text
    modal = body.split('id="modal-add-device"', 1)[1]
    assert modal.count('name="port"') == 1
    assert modal.count('name="code"') == 1
    assert "hx-swap-oob" not in modal


def test_an_unknown_type_on_the_partial_is_refused_not_substituted(
        monkeypatch):
    body = pair_client(monkeypatch).get(
        "/partials/device-type?model=NOPE").text
    assert "not a device type this install carries" in body


def test_reopening_the_form_asks_again_for_the_selected_types_fields():
    """
    `form.reset()` returns the dropdown to its first option and leaves
    whatever fields were last swapped in - so without this, reopening
    shows type A selected beside type B's set points.
    """
    index = (HERE / "templates" / "index.html").read_text(encoding="utf-8")
    show = index.split("form.reset()", 1)[1][:600]
    assert 'trigger(picker, "change")' in show


def test_the_type_partial_is_behind_the_same_permission_as_adding(
        monkeypatch):
    """It names the instruments this install carries, which is not public."""
    source = Path(A_MODULE.__file__).read_text(encoding="utf-8")
    body = source.split("def partial_device_type(", 1)[1][:500]
    assert "permissions.DEVICE_ADD" in body


def test_two_type_changes_cannot_land_out_of_order():
    """
    FOUND BY CHANGING THE TYPE TWICE QUICKLY in a browser. Both requests
    went out, the first answer landed last, and the form ended up
    showing one instrument selected beside another's set point fields,
    its port and its suggested code - the exact fault this partial
    exists to prevent, arriving by a different route.

    `abort` and not `replace`: replace swaps a QUEUED request and leaves
    the one already in flight to land, which is what was happening.
    """
    modal = (HERE / "templates" / "_modal.html").read_text(encoding="utf-8")
    picker = modal.split("<select", 1)[1].split(">", 1)[0]
    assert 'hx-sync="this:abort"' in picker
