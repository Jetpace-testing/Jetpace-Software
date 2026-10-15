"""
Environmental monitoring: ISO 14644, differential pressure, Part 11.

One of the two products over jetpace-core. What belongs here is anything
that knows a reading describes a controlled space: the `dp` parameter
key, what "in spec" and "reversed" mean for a pressure cascade, the
excursion arithmetic, the iDP-100 register map, and the pages that show
them.

This is the product that gets qualified at a customer site, which is why
architecture.md §2 wants it pinned to a core version it has validated.
That pin does not exist yet - see the note in pyproject.toml for why it
cannot be written there and what delivering it would take.

The web half is in `jetpace_environment.web`, which exposes a single
`product()` factory returning the routes, templates, stylesheet and nav
entry that core's shell composes. Core never imports this package by
name.

`DEVICE_TYPES` is here at the ROOT rather than in `.web`, and that is
load-bearing. Two things read it: the commissioning form, through
`Product.device_types`, and the POLLER, which needs the register maps to
know which devices in the database it can read at all. Keeping it here
means the poller imports this package and gets a register map, without
loading FastAPI and Jinja to find out where DP_PV lives - see
jetpace_core.inventory and jetpace_core.selection. Nothing in the import
chain below this line touches a socket, a database or a web framework.
"""

from jetpace_core.devices import DeviceType, Point

from .registers import IDP100

__all__ = ["DEVICE_TYPES"]

# ---------------------------------------------------------------------------
# what this product can commission
#
# The register map is in ../registers.py, pinned to the datasheet. What
# this adds is the product's decisions about it: that DP_PV is stored as
# the parameter `dp` in Pa with no decimals, that the unit selector is
# worth storing too, and that an operator's low and high set points apply
# to `dp` rather than to the selector.
#
# `dp_unit` is read and stored deliberately, even though nothing plots it.
# It says whether the instrument is reporting mmWC or Pa, and a transmitter
# whose unit register was changed on site - by a previous integrator, or by
# somebody with the vendor's configuration tool - would otherwise report
# numbers 9.8 times too small with nothing anywhere to say why. Storing it
# makes that answerable from the database instead of from a site visit.
#
# Scale stays 1.0 on both. Debt #6 is that DP_PV's scaling is unconfirmed
# against a real instrument, and a plausible factor invented here is how
# that becomes permanent and reaches a batch record. It is one number in
# one place when the cleanroom is on the bench.
# ---------------------------------------------------------------------------

DEVICE_TYPES = (
    DeviceType(
        model=IDP100,
        kind="pressure",
        label="Differential pressure transmitter",
        points=(
            Point("dp", "dp_pv", "Differential pressure", "Pa",
                  aggregation="instantaneous", decimals=0),
            Point("dp_unit", "unit", "Pressure unit selector", "",
                  aggregation="instantaneous", decimals=0),
        ),
        limits="dp",
    ),
)
