"""
The MQTT contract.

No broker, no network. Topics and payloads are pure functions precisely so
that the shape of what goes on the wire can be pinned down here, where it
is cheap, rather than discovered at a site.
"""

from datetime import datetime, timedelta, timezone

import pytest

from jetpace_core.mqtt import (
    QOS,
    ParsedTopic,
    Site,
    TopicError,
    decode_reading,
    decode_status,
    encode_reading,
    encode_status,
    parse,
)

SITE = Site(tenant="meril", site="vapi")
NOW = datetime(2026, 10, 7, 9, 15, tzinfo=timezone.utc)


# --- topics ----------------------------------------------------------------

def test_topic_shapes():
    assert SITE.telemetry("GW02-047", "kwh_import") == \
        "jetpace/meril/vapi/telemetry/GW02-047/kwh_import"
    assert SITE.status("GW02-047") == "jetpace/meril/vapi/status/GW02-047"
    assert SITE.agent_status == "jetpace/meril/vapi/status/agent"
    assert SITE.command("GW02-047") == "jetpace/meril/vapi/cmd/GW02-047"


def test_subscription_wildcards():
    assert SITE.all_telemetry == "jetpace/meril/vapi/telemetry/+/+"
    assert SITE.all_status == "jetpace/meril/vapi/status/+"
    assert SITE.everything == "jetpace/meril/vapi/#"


@pytest.mark.parametrize("bad", ["a/b", "a+b", "a#b", "", "has space"])
def test_wildcards_and_separators_are_refused(bad):
    """
    A device code containing + # or / would either be rejected by the
    broker or silently change the shape of the tree. Either way it is a
    configuration error, so it is refused rather than escaped.
    """
    with pytest.raises(TopicError):
        SITE.telemetry(bad, "dp")


def test_a_bad_site_name_fails_at_construction():
    with pytest.raises(TopicError):
        Site(tenant="meril/vapi", site="x")


# --- reading payloads ------------------------------------------------------

def test_reading_round_trip():
    ts, value, quality = decode_reading(encode_reading(NOW, 1234.56, 0))
    assert ts == NOW
    assert value == pytest.approx(1234.56)
    assert quality == 0


def test_reading_carries_negative_pressure():
    """The whole point of the signed decode must survive the wire too."""
    _, value, _ = decode_reading(encode_reading(NOW, -5.0))
    assert value == -5.0


def test_reading_is_utc_whatever_it_was_given():
    ist = timezone(timedelta(hours=5, minutes=30))
    payload = encode_reading(NOW.astimezone(ist), 12.0)
    assert b"+00:00" in payload
    ts, _, _ = decode_reading(payload)
    assert ts == NOW


def test_naive_timestamps_are_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_reading(datetime(2026, 10, 7, 9, 15), 12.0)


def test_quality_survives():
    _, _, q = decode_reading(encode_reading(NOW, 1.0, quality=1))
    assert q == 1


@pytest.mark.parametrize("payload, why", [
    (b"",                       "not JSON"),
    (b"not json",               "not JSON"),
    (b"[1,2,3]",                "not an object"),
    (b'{"v":1}',                "no 'ts'"),
    (b'{"ts":"2026-10-07T09:15:00+00:00"}', "no 'v'"),
    (b'{"ts":"nonsense","v":1}', "unparseable timestamp"),
    (b'{"ts":"2026-10-07T09:15:00","v":1}', "no timezone"),
    (b'{"ts":"2026-10-07T09:15:00+00:00","v":"x"}', "value not a number"),
])
def test_malformed_payloads_are_refused(payload, why):
    """
    Ingest consumes whatever the broker hands it, and a broker hands on
    whatever anybody published - including a half-written message from a
    crashing agent. Refusing here keeps the bad row out of the historian,
    which is the only place it would be expensive.
    """
    with pytest.raises(ValueError):
        decode_reading(payload)


# --- status payloads -------------------------------------------------------

def test_status_round_trip():
    online, detail, at = decode_status(encode_status(True, "answering", NOW))
    assert online is True
    assert detail == "answering"
    assert at == NOW


def test_status_offline():
    online, detail, _ = decode_status(encode_status(False, "no reply"))
    assert online is False
    assert detail == "no reply"


# --- parsing back ----------------------------------------------------------

def test_parse_telemetry():
    assert parse("jetpace/meril/vapi/telemetry/GW02-047/dp") == ParsedTopic(
        "meril", "vapi", "telemetry", "GW02-047", "dp")


def test_parse_status():
    assert parse("jetpace/meril/vapi/status/GW02-047") == ParsedTopic(
        "meril", "vapi", "status", "GW02-047", None)


@pytest.mark.parametrize("topic", [
    "something/else/entirely",
    "jetpace/meril",
    "jetpace/meril/vapi/telemetry/GW02-047",        # missing the metric
    "jetpace/meril/vapi/telemetry/a/b/c",           # one level too many
])
def test_foreign_topics_return_none_rather_than_raising(topic):
    """
    A subscriber on a shared broker will legitimately see traffic that is
    not ours. That is not an error; it is just not for us.
    """
    assert parse(topic) is None


def test_round_trip_through_a_topic():
    topic = SITE.telemetry("DP-001", "dp")
    p = parse(topic)
    assert p.tenant == SITE.tenant and p.site == SITE.site
    assert p.device == "DP-001" and p.metric == "dp"


def test_qos_is_at_least_once():
    """QoS 1 is what makes idempotent ingest mandatory rather than optional."""
    assert QOS == 1
