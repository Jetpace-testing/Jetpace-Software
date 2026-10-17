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

__all__ = ["DEVICE_TYPES"]

# ---------------------------------------------------------------------------
# what this product can commission
#
# Nothing, today. An empty tuple is a valid product: the form's device
# type list is built from every loaded product's types, and a product
# contributing none simply adds none.
#
# What goes here is one `DeviceType` per instrument - the register map
# from its datasheet, pinned by a test, plus this product's decisions
# about it: which registers become stored parameters, in what unit, at
# what scale, and which of them an operator's set points apply to.
#
# The scale is the number to get right. An instrument reporting tenths
# read as whole units is wrong by a factor of ten in every reading, every
# trend and every report, consistently and with nothing on screen to
# suggest it. It is one number in one place while the instrument is on
# the bench, and a site visit afterwards.
# ---------------------------------------------------------------------------

DEVICE_TYPES = ()
