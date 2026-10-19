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
from collections.abc import Mapping
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

# A PARAMETER KEY is the one identifier that reaches the most places. It is
# a column in a CSV export, a query string in /api/trend?param=, a
# data-param attribute the chart reads back, an MQTT topic segment, and -
# since set points became per-parameter - part of an HTML form field name
# (`low_temp`). Lowercase, digits and underscore are safe in all five
# without escaping; nothing validated it at all until instruments started
# sharing keys.
PARAM = re.compile(r"[a-z][a-z0-9_]{0,30}")

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
    # Whether an operator sets a specification on this parameter. A
    # cleanroom transmitter carries one on temperature AND on humidity,
    # while the unit selector beside them carries none.
    #
    # ON THE POINT, not a list of names on the type. A list would have to
    # be checked against the points - "every name in `limits` must be a
    # parameter this type reads" - and a flag makes that mistake
    # unrepresentable rather than caught. It also gives the set point
    # fields the same order as the reading tiles on the device page,
    # because both follow `points`.
    limited: bool = False

    def __post_init__(self) -> None:
        if not PARAM.fullmatch(self.param):
            raise ValueError(
                f"parameter key {self.param!r} is not usable: it becomes a "
                f"CSV column, a query string, an MQTT topic segment and a "
                f"form field name. Lowercase letters, digits and "
                f"underscore, starting with a letter.")
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
    map, what to read from it, and which of those parameters an operator
    sets a specification on.

    Which parameters carry set points is a property of the POINTS
    (`Point.limited`), not a list here. See the note on that field.
    """

    model: DeviceModel
    kind: str                           # device_model.kind
    points: tuple[Point, ...]
    label: str = ""
    # Optional: the product this is a variant of. Several models that
    # are one instrument with different sensors fitted share a family,
    # and the form groups them under it - so an operator finds the
    # product first and chooses the variant second, which is how the
    # people who sell them talk about them.
    #
    # A label and nothing more. Nothing is resolved through it and two
    # families are never compared; a typo makes a second heading on a
    # dropdown, not a device read at the wrong address.
    family: str = ""
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

    def limit_points(self) -> tuple[Point, ...]:
        """
        The parameters an operator sets a specification on, in point order.

        Point order and not alphabetical: the device page lays its reading
        tiles out in point order, and a form whose fields are in a
        different order from the page they describe is a form somebody
        fills in wrongly once.
        """
        return tuple(p for p in self.points if p.limited)


@dataclass(frozen=True)
class Planned:
    """
    An instrument this install will be able to commission, and cannot yet.

    DELIBERATELY NOT A HALF-BUILT DeviceType. The addresses to read come
    from a register map, so a type without one cannot be commissioned at
    all, and putting it in the dropdown would be an entry that throws on
    submit - the exact thing `DeviceType.__post_init__` exists to prevent.
    This carries a name and nothing else. The form renders it greyed and
    unselectable, so an engineer can see the catalogue is coming rather
    than wondering whether their instrument is supported.

    It never reaches `DEVICE_TYPES`, so the poller and `inventory.plan()`
    never see one: "declared but not transcribed" stays distinct from
    `Plan.unreadable`, which is a commissioned device nothing can read.

    What it is waiting for is in docs/instruments.md.
    """

    code: str                    # the model code it will have
    label: str                   # what the instrument is

    @property
    def title(self) -> str:
        return f"{self.code} \u00b7 {self.label}" if self.label else self.code


def check_parameters(types: Mapping[str, "DeviceType"]) -> None:
    """
    Two instruments may share a parameter key only if they agree about it.

    WHY THIS HAS TO EXIST BEFORE A SECOND INSTRUMENT DOES. The `parameter`
    table is keyed on `key` alone - one row for `temp` across the whole
    install - and commissioning upserts it with
    `ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label`. So the label
    is overwritten site-wide by whichever type was commissioned most
    recently, while the unit, the aggregation and the decimals are frozen
    by whichever type used the key FIRST and silently discarded
    afterwards.

    With one instrument in the repository that was unobservable. With a
    range of them sharing `temp`, `rh` and `flow`, commissioning an RTD
    after a cleanroom transmitter relabels the cleanroom's parameter on
    every page and throws away the RTD's own declared precision - a value
    rendered to the wrong number of decimals, consistently, with nothing
    on screen to suggest it.

    The fix is not a cleverer upsert: `DO NOTHING` returns no row and the
    insert needs its id back. It is this - make the disagreement
    impossible, so the UPDATE is always a no-op. Called from both places
    that collect types (the form's catalogue and the poller's), because
    they are reached by different processes and only one of them has a
    browser to report into.
    """
    seen: dict[str, tuple[str, Point]] = {}
    for model_code, device_type in sorted(types.items()):
        for point in device_type.points:
            first = seen.get(point.param)
            if first is None:
                seen[point.param] = (model_code, point)
                continue
            other_code, other = first
            differs = [
                name for name in ("label", "unit", "aggregation", "decimals")
                if getattr(point, name) != getattr(other, name)
            ]
            if differs:
                raise SystemExit(
                    f"{model_code} and {other_code} both read the parameter "
                    f"{point.param!r} and disagree about its "
                    f"{', '.join(differs)}.\n\n"
                    f"  There is one `parameter` row per key for the whole "
                    f"install, so the two cannot both be right:\n"
                    f"    {other_code}: {other.label!r} {other.unit!r} "
                    f"{other.aggregation} {other.decimals}dp\n"
                    f"    {model_code}: {point.label!r} {point.unit!r} "
                    f"{point.aggregation} {point.decimals}dp\n\n"
                    f"  Either agree, or give one of them its own key.")
