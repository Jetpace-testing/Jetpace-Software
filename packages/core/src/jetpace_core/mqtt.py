"""
The MQTT contract: topics and payloads.

Pure functions, no client, no network - so the shape of what goes on the
wire can be tested without a broker, and so both ends of the system agree
by construction rather than by two developers reading the same paragraph.

A topic tree is a schema. It is painful to change once sites are deployed,
which is why it is settled here rather than discovered later.

    jetpace/<tenant>/<site>/telemetry/<device>/<metric>   readings
    jetpace/<tenant>/<site>/status/<device>               retained + will
    jetpace/<tenant>/<site>/status/agent                  retained + will
    jetpace/<tenant>/<site>/cmd/<device>                  commands down

Rules that go with it:

  QoS 1, with idempotent ingest. At-least-once WILL deliver twice. Note
  also that QoS is client-to-broker, not end-to-end: a PUBACK says the
  broker holds the message, never that the historian wrote it.

  Retain status, never telemetry. A dashboard showing a three-day-old
  pressure reading as current is a hazard, not a convenience; live values
  come from the database, which already holds them.

  A will on every device and agent topic. That is what replaces a silence
  heuristic with an assertion.

  Plain JSON, not Sparkplug B - for now. Sparkplug means protobuf,
  sequence numbers and a birth/death state machine: real machinery for a
  benefit nobody has asked for yet. This hierarchy maps cleanly onto
  Sparkplug's group/node/device structure, so it can be adopted when a
  customer with an Ignition installation requires it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

PREFIX = "jetpace"
QOS = 1

# MQTT forbids + and # in a published topic, and a / would silently create
# an extra level. Anything that would change the shape of the tree is
# rejected rather than escaped - a device code is an identifier, and one
# containing a wildcard is a configuration error, not something to paper
# over.
_SEGMENT = re.compile(r"^[A-Za-z0-9._:-]+$")


class TopicError(ValueError):
    """A name that cannot safely be part of a topic."""


def _segment(value: str, what: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.match(value):
        raise TopicError(
            f"{what} {value!r} is not usable in a topic: expected letters, "
            "digits, dot, underscore, colon or hyphen"
        )
    return value


@dataclass(frozen=True)
class Site:
    """Which tenant and site this agent speaks for."""

    tenant: str
    site: str

    def __post_init__(self) -> None:
        _segment(self.tenant, "tenant")
        _segment(self.site, "site")

    @property
    def root(self) -> str:
        return f"{PREFIX}/{self.tenant}/{self.site}"

    # --- publishing ---------------------------------------------------

    def telemetry(self, device: str, metric: str) -> str:
        return (f"{self.root}/telemetry/{_segment(device, 'device code')}"
                f"/{_segment(metric, 'metric')}")

    def status(self, device: str) -> str:
        return f"{self.root}/status/{_segment(device, 'device code')}"

    @property
    def agent_status(self) -> str:
        return f"{self.root}/status/agent"

    def command(self, device: str) -> str:
        return f"{self.root}/cmd/{_segment(device, 'device code')}"

    # --- subscribing --------------------------------------------------

    @property
    def all_telemetry(self) -> str:
        return f"{self.root}/telemetry/+/+"

    @property
    def all_status(self) -> str:
        return f"{self.root}/status/+"

    @property
    def everything(self) -> str:
        return f"{self.root}/#"


# ---------------------------------------------------------------------------
# payloads
# ---------------------------------------------------------------------------

def encode_reading(ts: datetime, value: float, quality: int = 0) -> bytes:
    """
    One reading. Short keys because this is the message sent most often by
    several orders of magnitude - at 400 devices and ten parameters a
    minute, four characters saved per field is real bandwidth.
    """
    if ts.tzinfo is None:
        raise ValueError("reading timestamps must be timezone-aware")
    return json.dumps(
        {"ts": ts.astimezone(timezone.utc).isoformat(),
         "v": float(value),
         "q": int(quality)},
        separators=(",", ":"),
    ).encode("utf-8")


def decode_reading(payload: bytes) -> tuple[datetime, float, int]:
    """
    Parse a reading, refusing anything malformed.

    Ingest consumes whatever the broker hands it, and a broker will hand it
    whatever anybody published - including, one day, a half-written message
    from a crashing agent. Refusing here keeps the bad row out of the
    historian, which is the only place it would be expensive.
    """
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"payload is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("payload is not an object")
    for key in ("ts", "v"):
        if key not in obj:
            raise ValueError(f"payload has no {key!r}")
    try:
        ts = datetime.fromisoformat(obj["ts"])
        value = float(obj["v"])
    except (ValueError, TypeError) as exc:
        raise ValueError(f"payload field is not readable: {exc}") from exc
    if ts.tzinfo is None:
        raise ValueError("payload timestamp has no timezone")
    quality = obj.get("q", 0)
    if not isinstance(quality, int) or isinstance(quality, bool):
        raise ValueError("quality is not an integer")
    return ts, value, quality


def encode_status(online: bool, detail: str = "",
                  at: datetime | None = None) -> bytes:
    """A retained status message. Also the shape used for the will."""
    at = at or datetime.now(timezone.utc)
    return json.dumps(
        {"online": bool(online), "detail": detail,
         "at": at.astimezone(timezone.utc).isoformat()},
        separators=(",", ":"),
    ).encode("utf-8")


def decode_status(payload: bytes) -> tuple[bool, str, datetime | None]:
    obj = json.loads(payload)
    at = None
    if isinstance(obj.get("at"), str):
        try:
            at = datetime.fromisoformat(obj["at"])
        except ValueError:
            at = None
    return bool(obj.get("online")), str(obj.get("detail", "")), at


# ---------------------------------------------------------------------------
# reading a topic back
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedTopic:
    tenant: str
    site: str
    kind: str                 # 'telemetry' | 'status' | 'cmd'
    device: str | None
    metric: str | None


def parse(topic: str) -> ParsedTopic | None:
    """
    Pull the parts back out of a topic, or None if it is not one of ours.

    Returning None rather than raising: a subscriber may legitimately see
    traffic it does not recognise - another product on a shared broker,
    say - and that is not an error, it is just not for us.
    """
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != PREFIX:
        return None
    tenant, site, kind = parts[1], parts[2], parts[3]

    if kind == "telemetry" and len(parts) == 6:
        return ParsedTopic(tenant, site, kind, parts[4], parts[5])
    if kind in ("status", "cmd") and len(parts) == 5:
        return ParsedTopic(tenant, site, kind, parts[4], None)
    return None
