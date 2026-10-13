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
