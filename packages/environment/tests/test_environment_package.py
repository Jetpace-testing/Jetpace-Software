"""
That the workspace wiring works at all - see the note in
packages/energy/tests/test_energy_package.py for why this exists and why
the file names differ between packages.

The package is currently EMPTY: its differential pressure instrument and
the pages built around a pressure cascade were removed on 17 October 2026
when the first real site turned out to monitor temperature and humidity.
What these tests hold in place is the seam, which is the expensive part -
that core can still find this package by slug, that it describes itself,
and that an empty product is a valid one rather than a half-removed one.
"""


def test_the_package_imports():
    import jetpace_environment

    assert jetpace_environment.__doc__, "the package should say what it is for"


def test_the_product_can_see_core():
    from jetpace_core import paths

    assert paths.install_root().is_dir()


# --- the descriptor core composes -----------------------------------------

def test_the_product_describes_itself_to_the_shell():
    """
    The slug has to match the package name, because that is what core
    resolves a configured name through, and it is also the URL and the
    nav key.
    """
    from jetpace_environment.web import product

    p = product()
    assert p.slug == "environment"
    assert p.label == "Environment"


def test_every_other_contribution_stays_optional():
    """
    The product sells instruments and has no pages, no stylesheet and
    no overview tiles - and that is a valid Product, not a half-built
    one. A product with routes but no templates IS refused, by
    Product.__post_init__; this asserts the legitimate shape passes.
    """
    from jetpace_environment.web import product

    p = product()
    assert p.routes is None
    assert p.templates is None
    assert p.static is None and p.stylesheet is None
    assert p.tiles is None and p.events is None
    assert p.device_types, "it sells at least one instrument now"


# `test_the_package_names_no_instrument` stood here. It forbade the
# strings "idp", "dp_pv", "never_write" and "dp" anywhere in this
# package, to stop the differential pressure transmitter that was
# removed on 17 October creeping back a line at a time.
#
# It is retired on purpose, not worked around. The Cleanroom Display
# has a differential pressure channel, a real datasheet and a real
# customer, so `dp` is now a parameter this product measures. A guard
# that would have to be edited to let correct code through has stopped
# guarding anything.


# --- what it will sell, and cannot yet ------------------------------------

def test_the_product_declares_the_instruments_it_is_waiting_for():
    """
    Declared, not half-built. A `Planned` carries a name and nothing
    else, because the addresses to read come from a register map and a
    type without one cannot be commissioned at all - so the form shows
    these greyed rather than offering an entry that would be refused.

    What each is waiting for is a row in docs/instruments.md.
    """
    from jetpace_environment.web import product

    planned = {p.code for p in product().planned}
    assert {"DPT-100", "RTD-100"} <= planned


def test_nothing_planned_is_also_sold():
    """
    A transcribed instrument moves OUT of PLANNED. Leaving it in both
    would put the same model in the dropdown twice, once selectable and
    once greyed.
    """
    from jetpace_environment.web import product

    p = product()
    sold = {t.code for t in p.device_types}
    waiting = {x.code for x in p.planned}
    assert not (sold & waiting), sold & waiting
