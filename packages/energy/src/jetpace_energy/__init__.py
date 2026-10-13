"""
Energy management: tariffs, billing, demand, EnPI.

One of the two products over jetpace-core. What belongs here is anything
that knows a reading is electrical: the `kwh_import` parameter key, the
`consumption()` billing semantics, CT and PT ratios, and the pages that
show them.

What does not belong here is anything a cleanroom install would also
want. That goes to core - and if it turns out core already has it, it
should be used rather than copied, because the one thing worse than
product logic in the shared layer is two copies of shared logic in the
products.

The web half is in `jetpace_energy.web`, which exposes a single
`product()` factory returning the routes, templates and nav entry that
core's shell composes. Core never imports this package by name.
"""
