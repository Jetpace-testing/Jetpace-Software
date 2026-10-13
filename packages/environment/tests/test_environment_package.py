"""
That the workspace wiring works at all - see the note in
packages/energy/tests/test_energy_package.py for why this exists and why
the file names differ between packages.
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


def test_the_product_ships_its_page_and_its_stylesheet():
    """
    Both are what `build.py` needs an --add-data entry for. The Product
    constructor already refuses a directory that does not exist, so this
    is really asserting the files are named as the build expects.
    """
    from jetpace_environment.web import product

    p = product()
    assert (p.templates / "environment.html").is_file()
    assert (p.static / "environment.css").is_file()
    assert p.stylesheet == "environment.css"


def test_the_stylesheet_introduces_no_colour_of_its_own():
    """
    The palette has one home, core's :root. A product stylesheet may use
    the tokens and must not define a colour, or light and dark mode start
    diverging one page at a time.
    """
    from jetpace_environment.web import product

    css = (product().static / "environment.css").read_text(encoding="utf-8")
    for literal in ("#", "rgb(", "hsl("):
        # '#' appears in no rule here; var(--x) is the only colour source
        assert literal not in css, f"environment.css contains {literal!r}"
