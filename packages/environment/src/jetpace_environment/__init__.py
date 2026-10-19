"""
Environmental monitoring: controlled spaces, ISO 14644, Part 11.

One of the two products over jetpace-core. What belongs here is anything
that knows a reading describes a controlled space: the parameters it is
monitored on, what "in spec" means for them, the register maps of the
instruments that report them, and the pages that show them.

This is the product that gets qualified at a customer site, which is why
architecture.md §2 wants it pinned to a core version it has validated.
That pin does not exist yet - see the note in pyproject.toml for why it
cannot be written there and what delivering it would take.

**THE PACKAGE IS CURRENTLY EMPTY, AND THAT IS A DECISION.** It carried
one instrument, a differential pressure transmitter, and the pages built
around a pressure cascade. All of it was removed on 17 October 2026: the
first real site monitors temperature and relative humidity, and keeping a
pressure product alive to be half-rewritten would have meant commissioning
against a register map nobody at the site can check against an instrument
they own.

So the package stays, holding nothing, because the seam it occupies is the
thing that was expensive to build: core discovers it by slug, composes its
templates and static files, and offers its `DEVICE_TYPES` in the
commissioning form. Filling it in is adding a register map and a page.
Rebuilding this would be adding a package boundary to a running system.

`DEVICE_TYPES` is here at the ROOT rather than in `.web`, and that is
load-bearing. Two things read it: the commissioning form, through
`Product.device_types`, and the POLLER, which needs the register maps to
know which devices in the database it can read at all. Keeping it here
means the poller imports this package and gets a register map without
loading FastAPI and Jinja to find out where a register lives - see
jetpace_core.inventory and jetpace_core.selection. Nothing in the import
chain below this line touches a socket, a database or a web framework.
"""

from jetpace_core.devices import DeviceType, Planned, Point

from .registers import model

__all__ = ["DEVICE_TYPES", "PLANNED"]

# ---------------------------------------------------------------------------
# what this product can commission
#
# The Cleanroom Display, in its three variants. One instrument, one
# register map (registers.py), three models differing only in which
# sensors are fitted - so an operator picks the product and then the
# variant, which is how they are sold and how they are labelled on the
# wall.
#
# THE POINTS ARE DEFINED ONCE AND SHARED. Three variants reading
# temperature must agree about what `temp` means down to its decimals,
# because there is one `parameter` row per key for the whole install.
# `devices.check_parameters` refuses to start otherwise; defining them
# here means it never has to.
#
# SET POINT 1 IS THE LOW ONE, set point 2 the high. The datasheet does
# not say; the person who owns the instrument does, and it is written
# down in registers.py beside the addresses it decides.
#
# SCALE IS 1.0 HERE AND IS NOT THE SCALE USED. How many decimal places
# a channel reports is configurable per instrument, at 40022-40024, so
# it is read at commissioning and stored against the device in
# `device_parameter.scale`, which overrides this. A figure invented
# here would be the one that silently multiplies a room's history by
# ten on the one unit somebody had configured differently.
# ---------------------------------------------------------------------------

FAMILY = "Cleanroom Display"

TEMPERATURE = Point("temp", "temp_pv", "Temperature", "°C",
                    aggregation="instantaneous", decimals=1, limited=True)
HUMIDITY = Point("rh", "rh_pv", "Relative humidity", "%RH",
                 aggregation="instantaneous", decimals=1, limited=True)
PRESSURE = Point("dp", "dp_pv", "Differential pressure", "Pa",
                 aggregation="instantaneous", decimals=1, limited=True)


def _variant(code: str, label: str, kind: str, points) -> DeviceType:
    return DeviceType(
        model=model(code),
        kind=kind,
        label=label,
        family=FAMILY,
        points=points,
        code_prefix="CR",
        # `critical`: a cleanroom is qualified on these readings. That
        # is this product's call and not the platform's.
        scan_class="critical",
    )


DEVICE_TYPES = (
    _variant("iCRDN-230-RT", "temperature and humidity", "temperature",
             (TEMPERATURE, HUMIDITY)),
    _variant("iCRDN-230-D", "differential pressure", "pressure",
             (PRESSURE,)),
    _variant("iCRDN-230-DRT", "pressure, temperature and humidity",
             "temperature", (PRESSURE, TEMPERATURE, HUMIDITY)),
)


# ---------------------------------------------------------------------------
# what this product will sell, and cannot yet
#
# Declared here so the form can show them greyed: an engineer looking for
# their transmitter finds out it is coming rather than concluding it is
# unsupported. They carry a name and nothing else, because the addresses
# to read come from a register map and there is no half of one worth
# having - see jetpace_core.devices.Planned.
#
# Each row of docs/instruments.md says which datasheet it is waiting for.
# When one is transcribed it moves from here to DEVICE_TYPES, and its
# pinning test arrives with it.
# ---------------------------------------------------------------------------

PLANNED = (
    Planned("DPT-100", "Differential pressure transmitter"),
    Planned("RTD-100", "RTD temperature probe"),
)
