"""
What devices exist, and where to read each value from.

Two halves of one question, which is why they are in one module.

  WHICH DEVICES. From the database: gateway rows, device rows, and the
  parameters each device has enabled. This is the half that moves
  (architecture.md debt #3, §10 step 4) - it has been in
  config/prototype.toml, which means commissioning a device has meant
  editing a file on the plant PC and restarting a service.

  WHERE TO READ IT. From the register map in the product package that
  sells the instrument, reached by the model code the device row carries.
  This half is NOT in the database and should not be: an address is a
  fact about a datasheet, pinned to it by a test, and a register map
  somebody can edit with an UPDATE statement is a register map that can
  silently stop matching the document it was transcribed from.

So a device row names a model and the product's map says where that
model's registers are; the database contributes which parameters are on
and the site's own scaling, and the two are composed here.

WHAT HAPPENS TO A DEVICE WHOSE MODEL HAS NO MAP. It cannot be polled -
there is nothing to say which register to read. It is counted and named
in `Plan.unreadable` rather than dropped, because a device that is
configured and silently not polled is the worst outcome available: the
dashboard shows it offline and nothing anywhere says why. This is a real
case, not a defensive one - an install serving only `environment` has no
map for an energy meter, and config/products.toml decides that.

NO GATEWAYS IS NOT AN ERROR HERE, unlike `cli.load_config`, which exits
when the file describes none. A fresh install has nothing commissioned
yet and the entire point of the form is that you commission from the
dashboard; a poller that refused to start until somebody had edited a
file would defeat it. It starts, polls nothing, and picks the first
device up on its next reload.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .devices import DeviceType
from .poller import DeviceSpec, GatewaySpec, PointSpec

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# the register maps the loaded products bring
# ---------------------------------------------------------------------------

# The attribute each product package exposes at its ROOT - not under
# .web. The commissioning form reaches the same tuple through
# web.products.Product.device_types, but the poller must not have to
# import a web module to find out what a register looks like, and
# importing one would pull FastAPI and Jinja into the poller process to
# answer a question about Modbus.
CATALOGUE_ATTR = "DEVICE_TYPES"


def load_device_types(slugs: Sequence[str]) -> dict[str, DeviceType]:
    """
    Every instrument the named products sell, keyed by model code.

    Quiet about a product that has no catalogue - energy has none today,
    because there is no meter register map in the repository - and quiet
    about one that will not import, because this is also called from the
    poller, where a missing product means fewer readable devices rather
    than a dead process. `Plan.unreadable` is where that becomes visible.

    Loud about two products claiming one model code, which is the same
    SystemExit `web.products.device_types` raises and for the same
    reason: it means they disagree about an instrument's registers, and
    resolving it by load order would decide which datasheet a site reads
    its devices with.
    """
    found: dict[str, DeviceType] = {}
    origin: dict[str, str] = {}
    for slug in slugs:
        try:
            module = importlib.import_module(f"jetpace_{slug}")
        except ImportError as exc:
            log.warning("no register maps from %r: %s", slug, exc)
            continue
        for device_type in getattr(module, CATALOGUE_ATTR, ()) or ():
            if device_type.code in found:
                raise SystemExit(
                    f"the {origin[device_type.code]!r} and {slug!r} products "
                    f"both describe the device model {device_type.code!r}. "
                    f"One register map per model, or a device would be read "
                    f"with whichever product happened to load first.")
            found[device_type.code] = device_type
            origin[device_type.code] = slug
    return found


# ---------------------------------------------------------------------------
# what the poller should be doing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    """
    The device list as it stands, plus what could not be made sense of.

    Frozen and made of frozen parts all the way down, which is what lets
    the poller decide whether anything changed by comparing two of these
    rather than by asking the database for a version number. A
    fingerprint query is a second thing that can be wrong; equality
    cannot disagree with the data it was computed from.
    """

    gateways: tuple[GatewaySpec, ...] = ()
    unreadable: tuple[str, ...] = ()

    @property
    def devices(self) -> int:
        return sum(len(g.devices) for g in self.gateways)

    @property
    def points(self) -> int:
        return sum(len(d.points) for g in self.gateways for d in g.devices)

    def summary(self) -> str:
        text = (f"{len(self.gateways)} gateway(s), {self.devices} device(s), "
                f"{self.points} point(s)")
        if self.unreadable:
            text += f", {len(self.unreadable)} NOT READABLE"
        return text


GATEWAYS = """
SELECT g.id,
       g.label,
       g.host,
       g.port,
       g.timeout_ms,
       g.gap_ms,
       g.scan_interval_ms,
       g.offline_backoff_cycles
FROM   gateway g
WHERE  g.enabled
ORDER  BY g.host, g.port
"""

# One row per (device, enabled parameter). `dp.scale` is the site's own
# override and is usually null, in which case the point's own scale from
# the register catalogue applies - which is how a per-device correction
# is possible without one being required.
#
# Ordered so that two reads of an unchanged database produce two equal
# Plans. Equality is the change detector, so a nondeterministic order
# here would be a reload on every poll.
DEVICE_PARAMETERS = """
SELECT d.gateway_id,
       d.code,
       d.name,
       d.unit_id,
       m.code  AS model,
       p.key   AS param,
       dp.scale
FROM   device d
JOIN   device_model m      ON m.id = d.model_id
JOIN   device_parameter dp ON dp.device_id = d.id AND dp.enabled
JOIN   parameter p         ON p.id = dp.param
WHERE  d.retired_at IS NULL
  AND  d.enabled
ORDER  BY d.gateway_id, d.unit_id, d.code, p.key
"""


def plan(conn, types: Mapping[str, DeviceType]) -> Plan:
    """
    Compose the device list from the database and the register maps.

    Reads two queries rather than one join of five tables, because the
    second has one row per parameter and the first would repeat every
    gateway's columns across them. Both are small - a 400-meter site is
    about 2,000 rows - and this runs on a reload interval, not per
    reading.
    """
    with conn.cursor() as cur:
        cur.execute(GATEWAYS)
        gateway_rows = cur.fetchall()
        cur.execute(DEVICE_PARAMETERS)
        parameter_rows = cur.fetchall()

    # (gateway_id, device code) -> the device's row plus its parameters
    grouped: dict[tuple[int, str], dict] = {}
    for row in parameter_rows:
        key = (row["gateway_id"], row["code"])
        entry = grouped.get(key)
        if entry is None:
            entry = grouped[key] = {
                "name": row["name"], "unit_id": row["unit_id"],
                "model": row["model"], "scales": {},
            }
        entry["scales"][row["param"]] = row["scale"]

    unreadable: list[str] = []
    by_gateway: dict[int, list[DeviceSpec]] = {}

    for (gateway_id, code), entry in grouped.items():
        device_type = types.get(entry["model"])
        if device_type is None:
            unreadable.append(
                f"{code}: no register map for model {entry['model']!r} - is "
                f"the product that sells it enabled in config/products.toml?")
            continue

        points: list[PointSpec] = []
        for point in device_type.points:
            if point.param not in entry["scales"]:
                continue          # the site turned this parameter off
            register = device_type.register_for(point)
            override = entry["scales"][point.param]
            points.append(PointSpec(
                param=point.param,
                address=register.address,
                kind=register.kind,
                scale=point.scale if override is None else float(override),
            ))

        if not points:
            unreadable.append(
                f"{code}: every parameter of model {entry['model']!r} is "
                f"disabled, so polling it would store nothing.")
            continue

        by_gateway.setdefault(gateway_id, []).append(DeviceSpec(
            code=code, name=entry["name"], unit_id=entry["unit_id"],
            points=tuple(points)))

    gateways = tuple(
        GatewaySpec(
            label=row["label"], host=row["host"], port=row["port"],
            timeout_s=row["timeout_ms"] / 1000.0,
            gap_s=row["gap_ms"] / 1000.0,
            interval_s=row["scan_interval_ms"] / 1000.0,
            offline_backoff_cycles=row["offline_backoff_cycles"],
            devices=tuple(sorted(by_gateway.get(row["id"], ()),
                                 key=lambda d: (d.unit_id, d.code))),
        )
        for row in gateway_rows
    )
    return Plan(gateways=gateways, unreadable=tuple(sorted(unreadable)))


# ---------------------------------------------------------------------------
# reading it, repeatedly, from a process that must not die of a hiccup
# ---------------------------------------------------------------------------

@dataclass
class DatabaseInventory:
    """
    The device list, re-read on demand.

    Holds ONE connection and rebuilds it after a failure, rather than
    connecting per read: the poller asks every few seconds for the life of
    the install, and `queries.connect` has no pool (debt #19).

    A failed read raises. The caller's job is to keep polling the devices
    it already has and try again - losing contact with the database must
    not stop a bus being read, because the readings are buffered and the
    device list changes once a month.
    """

    dsn: str
    types: Mapping[str, DeviceType]
    _conn: object | None = field(default=None, repr=False)

    def read(self) -> Plan:
        import psycopg
        from psycopg.rows import dict_row

        if self._conn is None or getattr(self._conn, "closed", True):
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row,
                                         autocommit=True, connect_timeout=10)
        try:
            return plan(self._conn, self.types)
        except Exception:
            # A connection that errored may be in a failed transaction or
            # simply gone. Drop it so the next read builds a fresh one,
            # rather than retrying forever on a socket the server closed
            # while the service was restarted underneath us.
            self.close()
            raise

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()           # type: ignore[attr-defined]
            except Exception:          # noqa: BLE001
                pass
