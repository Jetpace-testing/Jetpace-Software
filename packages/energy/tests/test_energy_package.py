"""
That the workspace wiring works at all.

Thin on purpose, and worth having: if `pythonpath` or `testpaths` in the
root pyproject.toml misses this package, the symptom is not a failure but
a suite that quietly runs fewer tests than it did yesterday. Something
here has to fail when the package is not importable.

Test file names are unique across the three packages deliberately. None
of the tests directories has an __init__.py, so pytest derives a module
name from the basename; two files called test_queries.py in two packages
would collide with "import file mismatch".
"""


def test_the_package_imports():
    import jetpace_energy

    assert jetpace_energy.__doc__, "the package should say what it is for"


def test_the_product_can_see_core():
    """
    The dependency direction that is allowed. A product may use the
    platform; architecture.md §3 and the import guards in core's tests
    cover the direction that is not.
    """
    from jetpace_core import paths

    assert paths.install_root().is_dir()


# --- the descriptor core composes -----------------------------------------

def test_the_product_describes_itself_to_the_shell():
    from jetpace_energy.web import product

    p = product()
    assert p.slug == "energy"
    assert p.label == "Energy"


def test_the_product_ships_its_page():
    from jetpace_energy.web import product

    assert (product().templates / "energy.html").is_file()


def test_the_product_declares_no_stylesheet():
    """
    Deliberate: this product's chart code is inline in its template and
    it uses no CSS core does not already define. Asserted rather than
    left implicit, because the wrong way to fix a missing rule would be
    to add it to core's app.css.
    """
    from jetpace_energy.web import product

    p = product()
    assert p.stylesheet is None
    assert p.static is None


# --- the exceptions this product contributes ------------------------------

def test_the_product_contributes_the_periods_it_cannot_defend():
    """
    architecture.md §7: a figure the system cannot defend comes back with
    a status rather than a plausible guess. consumption() already decides
    that; this surfaces the decision where somebody who has to fix a
    meter before the month closes will see it.
    """
    from jetpace_energy.web import product

    assert product().events is not None


def test_every_undefendable_status_has_a_sentence():
    """
    An operator reading "counter_reset" learns nothing. Each status
    consumption() can return needs words, and those words are this
    product's - the shell has no business knowing what a counter reset
    is.
    """
    from jetpace_energy.web import DEFENSIBLE, WHY

    # The statuses in migrations/0003_consumption_guard.sql, less the
    # one that means the figure is fine.
    for status in ("counter_reset", "partial_new_meter", "stale_open",
                   "not_a_counter"):
        assert status in WHY, f"{status} would be shown raw to an operator"
    assert DEFENSIBLE not in WHY
