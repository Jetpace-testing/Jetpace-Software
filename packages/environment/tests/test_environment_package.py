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


def test_an_empty_product_is_a_valid_one():
    """
    No instruments, no pages, no stylesheet, no overview tiles. Every one
    of those is optional on Product, and this is the state the package is
    in until its next instrument arrives.

    It matters that this is CHECKED rather than merely true: a product
    that half-exists - routes with no templates, a stylesheet with no
    static directory - is refused at import by Product.__post_init__, and
    this asserts the empty case passes that gate instead of tripping it.
    """
    from jetpace_environment.web import product

    p = product()
    assert p.device_types == ()
    assert p.routes is None
    assert p.templates is None
    assert p.static is None and p.stylesheet is None
    assert p.tiles is None and p.events is None


def test_the_package_names_no_instrument():
    """
    "Forget the datasheets and registers." A stray reference to the
    transmitter that used to live here - in a docstring, a parameter key,
    a register name - is how a deleted instrument comes back as a thing
    somebody half-implements against.

    The package docstring is allowed to say what was removed and why.
    Nothing else is.
    """
    from pathlib import Path

    import jetpace_environment

    root = Path(jetpace_environment.__file__).parent
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path.name == "__init__.py" and path.parent == root:
            # The one file that explains the removal.
            source = source.split('"""', 2)[-1]
        lowered = source.lower()
        for gone in ("idp", "dp_pv", "never_write", "'dp'", '"dp"'):
            assert gone not in lowered, f"{path.name} still mentions {gone}"
