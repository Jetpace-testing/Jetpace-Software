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
"""
