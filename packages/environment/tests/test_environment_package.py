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
