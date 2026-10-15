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

from jetpace_core.web import products as P
from jetpace_core.web import queries as q
from jetpace_core.web import writes as w
from jetpace_core.web.app import HERE, create_app

DSN = "postgresql://unused:unused@127.0.0.1:1/unused"
TEMPLATES = HERE / "templates"


def product():
    """
    The real environment product.

    The form is made OF what a product contributes, so a fabricated one
    would be testing the fabrication rather than the seam. What is
    asserted here is the mechanism; the iDP-100's registers are pinned
    against the datasheet in packages/environment.
    """
    return P.load(["environment"])


class Answered:
    reachable, answered, detail = True, True, ""

    class _V:
        def __init__(self, param, value):
            self.param, self.value = param, value

    values = (_V("dp", 12.4), _V("dp_unit", 1.0))


class Silent:
    reachable, answered = True, False
    values = ()
    detail = "the converter answered, but unit 9 did not."


class Unreachable:
    reachable = answered = False
    values = ()
    detail = "nothing answered at 10.0.0.9:502."


@pytest.fixture
def client(monkeypatch):
    """The overview, with every query it runs answered in memory."""
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
            "model": "iDP-100", "code": "DP-010", "name": "Gowning room",
            "low": "-20", "high": "-5"}
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
        "model": "iDP-100"}).text
    assert "answered" in body
    assert "Differential pressure" in body
    assert "12" in body


def test_the_reading_is_shown_to_the_decimals_the_product_declared():
    """
    `dp` is declared with decimals=0, because a cleanroom cascade is read
    in whole pascals. "12.40 Pa" on a commissioning screen implies a
    precision the instrument does not have.
    """
    from jetpace_environment import DEVICE_TYPES
    assert DEVICE_TYPES[0].points[0].decimals == 0


def test_a_rounded_reading_does_not_show_a_decimal_point(monkeypatch, client):
    set_probe(monkeypatch, Answered)
    body = client.post("/partials/probe", data={
        "host": "127.0.0.1", "port": "5021", "unit_id": "5",
        "model": "iDP-100"}).text
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
        "model": "iDP-100"}).text
    assert "The converter answered" in silent
    assert Silent.detail in silent, "the probe's own explanation is shown"

    set_probe(monkeypatch, Unreachable)
    gone = client.post("/partials/probe", data={
        "host": "10.0.0.9", "port": "502", "unit_id": "1",
        "model": "iDP-100"}).text
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
        "model": "iDP-100"}).text
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
        "model": "iDP-100"})

    from jetpace_core import inventory
    device_type = inventory.load_device_types(["environment"])["iDP-100"]
    expected = [(p.param, device_type.register_for(p).address,
                 device_type.register_for(p).kind)
                for p in device_type.points]
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
    assert "dp=12" in captured["detail"]


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
        "model": "iDP-100", "code": "DP-010", "name": "x"}).text
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
    assert "iDP-100" in body, "it should say what this install does carry"


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
