"""
The environmental product's pages.

Exactly one public name, `product()`, which is what core's shell calls to
find out what this package contributes. Core discovers this module by a
slug read from configuration, so core contains no import of it - see
jetpace_core.web.products for why that is done by name rather than by
entry point.

**IT CONTRIBUTES NOTHING TODAY.** The pages that lived here - a cleanroom
summary, a 24-hour trend, an excursion list, three overview tiles - were
all expressed in terms of a pressure cascade, in SQL that named the `dp`
parameter in five places. The product's instrument was removed on 17
October 2026 (see the package docstring) and the pages went with it
rather than being left to query a parameter that no longer exists
anywhere and render three dashes.

A product with no routes gets no navigation entry. That is deliberate and
it follows the rule the shell's own nav already states: an item that 404s
is worse than one that is visibly not there yet.

What a page needs in order to come back is a specification to draw -
which parameters a controlled space is held to, and what in-spec means
for them. That is this package's knowledge and nobody else's, which is
why core's overview and device pages carry on working perfectly well
without it: they show whatever parameters a device actually reports,
and they judge none of them.
"""

from __future__ import annotations

from jetpace_core.web.products import Product

from .. import DEVICE_TYPES


def product() -> Product:
    return Product(
        slug="environment",
        label="Environment",
        device_types=DEVICE_TYPES,
    )
