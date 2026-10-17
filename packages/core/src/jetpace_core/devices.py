"""
How a device model is described, for any instrument on any bus.

The scaffolding half of what used to be `registers.py`: that file held
both this and one product's register map, and a map belongs to the
package that sells the instrument. What stays here is the part that is
true of a Modbus device regardless of what it measures - which is why
this survived the removal of the only instrument the repository had.

Four things, in two layers. `wire_address`, `Register` and `DeviceModel`
describe a transcribed datasheet - what an instrument has. `Point` and
`DeviceType` describe what to do with one: which registers are worth
storing, what they mean once stored, and which parameter an operator's
set points apply to. The commissioning form is built from the second
layer and the poller reads addresses out of the first.

The 4xxxx numbers in the datasheets are the traditional notation for
holding registers. The wire address is the number minus 40001, so a value
documented at 40001 is read with start address 0. Getting this off by one
is the single most common Modbus mistake, which is why the conversion
lives in one place, is tested, and is listed as an invariant in
CLAUDE.md - a platform rule, not one product's.

Imports nothing of ours and nothing that touches a socket, a database or
a clock, so every test over it runs with none of those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def wire_address(datasheet_number: int) -> int:
    """40001 -> 0. The address actually sent on the wire."""
    return datasheet_number - 40001


@dataclass(frozen=True)
class Register:
    """One readable value in a device's map."""

    key: str
    datasheet_number: int
    kind: str  # 's16' | 'u16' | 'f32'
    unit: str
    label: str
    writable: bool = False

    @property
    def address(self) -> int:
        return wire_address(self.datasheet_number)


@dataclass(frozen=True)
class DeviceModel:
    """
    A transcribed datasheet.

    Every map built from this carries the file it came from. A datasheet
    revision that moves a register gets a NEW model with a new revision
    string, never an edit to an existing one, so that history stays
    decodable: a reading taken last year has to be interpretable with the
    map that was in force when it was taken.
    """

    code: str
    revision: str
    vendor: str
    datasheet_ref: str
    registers: tuple[Register, ...]

    def by_key(self, key: str) -> Register:
        for r in self.registers:
            if r.key == key:
                return r
        raise KeyError(f"{self.code} has no register {key!r}")


# ---------------------------------------------------------------------------
# a device somebody can commission
#
# `DeviceModel` above describes a transcribed datasheet: what registers an
# instrument has. That is not enough to commission one from a form, which
# also needs to know which of those registers are worth storing, what the
# values MEAN in the database - a parameter key, a label, a unit, whether
# it is a counter - and which parameter the operator's set points apply to.
#
# Those two things are deliberately separate. A datasheet is a fact about
# an instrument and a new revision is a new DeviceModel, never an edit;
# the decision to store a given register as a named parameter, in a given
# unit, and to put set points on that one rather than another, is a
# product decision that can change without the datasheet changing.
#
# This lives in core because it is true of any Modbus device, and is
# contributed BY a product through `web.products.Product.device_types`, so
# core still names no product and the type dropdown on the commissioning
# form is whatever the loaded products actually sell.
#
# Both validate in __post_init__, so a product with a typo fails when the
# application starts rather than when a customer fills in the form. The
# alternative is a dropdown whose entries throw on submit.
# ---------------------------------------------------------------------------

# Mirrors the CHECK on device_model.kind in migrations/0001. Duplicated
# rather than read from the database because this is validated at import,
# long before a connection exists - and pinned by a test that reads the
# migration, so the two cannot drift.
KINDS = ("energy", "pressure", "temperature", "humidity",
         "particle", "gas", "flow", "velocity")

# A device code reaches three places that constrain it: the URL
# /devices/<code>, an MQTT topic segment, and a CSV export column. Letters,
# digits and hyphens are safe in all three without escaping, and escaping
# an identifier differently in three places is how one of them ends up
# wrong.
CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,30}")
CODE_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,15}")

# Mirrors the CHECK on parameter.aggregation, likewise.
AGGREGATIONS = ("counter", "instantaneous")

# Mirrors the CHECK on device.scan_class, likewise.
SCAN_CLASSES = ("counter", "instantaneous", "critical")


@dataclass(frozen=True)
class Point:
    """
    One register worth storing, and what it means once stored.

    `register` names a Register in the model by its key, rather than
    repeating an address: an address written twice is an address that can
    disagree with itself, and the 40001 offset is the most common Modbus
    mistake there is. The wire address comes from the datasheet
    transcription or it does not come from anywhere.

    The parameter metadata is here because commissioning a device may be
    the first time a site has ever seen this parameter, and the
    `parameter` row has to be created with a label, a unit and an
    aggregation. Carrying them on the point means the product that sells
    the instrument decides them, in one place, instead of a seed script.
    """

    param: str                   # parameter.key - what the poller speaks
    register: str                # Register.key in the model
    label: str                   # parameter.label - what a person reads
    unit: str                    # parameter.unit, "" for a selector
    aggregation: str = "instantaneous"
    decimals: int = 2
    # Overrides the model's own scaling for this site. 1.0 means the raw
    # decoded value is the value, which is the honest default - and the
    # number to check against a real instrument before a site goes live.
    # Many transmitters report tenths; read as whole units, every reading,
    # trend and report is out by a factor of ten, consistently, with
    # nothing on screen to suggest it.
    scale: float = 1.0

    def __post_init__(self) -> None:
        if self.aggregation not in AGGREGATIONS:
            raise ValueError(
                f"point {self.param!r} has aggregation "
                f"{self.aggregation!r}; the rollups understand "
                f"{' and '.join(AGGREGATIONS)} and nothing else.")
        if self.decimals < 0:
            raise ValueError(f"point {self.param!r} has negative decimals.")


@dataclass(frozen=True)
class DeviceType:
    """
    Something a customer can choose on the commissioning form.

    One of these is what "device type" means in the dropdown: a register
    map, what to read from it, and which parameter the low and high set
    points apply to.
    """

    model: DeviceModel
    kind: str                           # device_model.kind
    points: tuple[Point, ...]
    # Which parameter LSP and HSP apply to, or None for a type that has no
    # single obvious one. The form shows the set point fields only when
    # this is set, labelled with that parameter's unit - "LSP (Pa)" rather
    # than a bare number whose unit the operator has to assume.
    limits: str | None = None
    label: str = ""
    # A serial-to-TCP converter in Modbus-gateway mode passes the slave
    # address through on one port, which is 502 by convention. In
    # transparent mode it is one port per device instead, and the operator
    # types the port - so this is a default on the form, not a constant.
    default_port: int = 502
    # What the form SUGGESTS as a device code: this plus the lowest free
    # number, "DP-004". A suggestion and not a rule, because the code is
    # stable forever and the site's own naming wins - but a suggestion
    # that matches the devices already there is worth a great deal when
    # somebody is commissioning the fourth of thirty.
    #
    # Here rather than derived from the model code, because deriving a
    # prefix from a vendor's model string needs a rule about leading
    # lowercase letters, hyphens and digits that is wrong for the next
    # instrument.
    code_prefix: str = ""
    # device.scan_class. 'critical' for an instrument whose reading is
    # the product's reason for existing, which is a product's call: a
    # pressure cascade in a sterile corridor is not a trend, it is the
    # thing the room is qualified on.
    scan_class: str = "instantaneous"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(
                f"device type {self.model.code!r} has kind {self.kind!r}; "
                f"device_model.kind accepts {', '.join(KINDS)}.")
        if not self.points:
            raise ValueError(
                f"device type {self.model.code!r} reads nothing. A type with "
                f"no points would commission a device that is polled and "
                f"never stores a value.")
        for point in self.points:
            # Raises KeyError naming the model and the key, which is what
            # a product author needs to see.
            self.model.by_key(point.register)
        params = [p.param for p in self.points]
        if len(set(params)) != len(params):
            raise ValueError(
                f"device type {self.model.code!r} maps two registers to the "
                f"same parameter; the reading table's primary key is "
                f"(device, parameter, time), so one would overwrite the "
                f"other.")
        if self.limits is not None and self.limits not in params:
            raise ValueError(
                f"device type {self.model.code!r} puts limits on "
                f"{self.limits!r}, which it does not read.")
        if not 1 <= self.default_port <= 65535:
            raise ValueError(
                f"device type {self.model.code!r} has default port "
                f"{self.default_port}.")
        if self.scan_class not in SCAN_CLASSES:
            raise ValueError(
                f"device type {self.model.code!r} has scan class "
                f"{self.scan_class!r}; device.scan_class accepts "
                f"{', '.join(SCAN_CLASSES)}.")
        if self.code_prefix and not CODE_PREFIX.fullmatch(self.code_prefix):
            raise ValueError(
                f"device type {self.model.code!r} suggests codes beginning "
                f"{self.code_prefix!r}. A device code is used in a URL and "
                f"in an MQTT topic, so the prefix has to be letters, digits "
                f"and hyphens.")

    @property
    def code(self) -> str:
        return self.model.code

    @property
    def revision(self) -> str:
        return self.model.revision

    @property
    def title(self) -> str:
        """What the dropdown shows. Falls back to the model code."""
        return f"{self.model.code} \u00b7 {self.label}" if self.label \
            else self.model.code

    def register_for(self, point: Point) -> Register:
        """The transcribed register a point reads. The address comes from here."""
        return self.model.by_key(point.register)

    def limit_point(self) -> Point | None:
        """The point LSP and HSP apply to, so the form can label their unit."""
        if self.limits is None:
            return None
        return next(p for p in self.points if p.param == self.limits)
