"""
That the product code actually left, asserted from core's side.

test_web_shell.py already says the five shell queries name no parameter
key. That is necessary and not sufficient: it says nothing about a sixth
query, and nothing about whether the product queries were moved out or
merely moved along. These tests scan core's source instead, so a symbol
that was renamed rather than relocated cannot satisfy them.

WHAT IS DELIBERATELY NOT IN SCOPE, so that the next person does not
"fix" the gap by widening the glob:

  - migrations/. 0002 defines consumption() and 0003-0004 rewrite it, and
    those are energy's. They cannot move: migrations are numbered,
    forward-only and already applied at every install. Core's SCHEMA owns
    an energy function permanently; core's PYTHON does not call it, and
    that is what is checked below.

  - config/prototype.toml. Its three devices are all pressure
    transmitters. It is site configuration rather than package code, and
    architecture.md §6 already has the device list moving into the
    database - building a per-product config mechanism now would be work
    paid for twice.

Both exceptions are recorded in architecture.md §9 rather than left for
someone to rediscover here.
"""

import ast
from pathlib import Path

import pytest

import jetpace_core

CORE_SRC = Path(jetpace_core.__file__).resolve().parent

# The two parameter keys that identify a product. A query containing
# either can only ever serve one product.
PRODUCT_KEYS = ("dp", "kwh_import")

SOURCES = sorted(CORE_SRC.rglob("*.py"))


def string_literals(path: Path) -> list[str]:
    """
    Every string literal in a file except docstrings.

    Comments are already excluded, because ast discards them. Docstrings
    are excluded on purpose: queries.py and app.py both explain at length
    why 'dp' and 'kwh_import' do not belong in them, and that prose is
    worth more than a scan that would forbid writing it down.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                docstrings.add(id(value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and id(n) not in docstrings]


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_there_are_core_sources_to_scan():
    """So that a glob that silently matches nothing cannot pass the rest."""
    assert len(SOURCES) > 10


@pytest.mark.parametrize("key", PRODUCT_KEYS)
def test_no_product_parameter_key_appears_in_core(key):
    quoted = f"'{key}'"
    offenders = [f"{p.relative_to(CORE_SRC)}: {text!r}"
                 for p in SOURCES
                 for text in string_literals(p)
                 if quoted in text]
    assert not offenders, (
        f"core still contains the {key!r} parameter key:\n  "
        + "\n  ".join(offenders))


def test_core_python_never_calls_the_billing_function():
    """
    consumption() is energy's, and core's schema is stuck with defining
    it (see the module docstring). Core must still not call it.
    """
    offenders = [str(p.relative_to(CORE_SRC)) for p in SOURCES
                 for text in string_literals(p) if "consumption(" in text]
    assert not offenders, f"core calls consumption() in {offenders}"


def test_core_imports_no_product_package():
    """
    The one-way rule from architecture.md §3, checked generically so that
    a third product needs no edit here - and so that this test does not
    itself have to name a product.
    """
    offenders = {}
    for path in SOURCES:
        theirs = {n for n in imported_names(path)
                  if n.startswith("jetpace")
                  and n != "jetpace_core"
                  and not n.startswith("jetpace_core.")}
        if theirs:
            offenders[str(path.relative_to(CORE_SRC))] = theirs
    assert not offenders, f"core imports product packages: {offenders}"


def test_core_stylesheet_carries_no_product_selector():
    css = (CORE_SRC / "web" / "static" / "app.css").read_text(encoding="utf-8")
    # Each of these was used by exactly one template, environment.html.
    for selector in (".barrow", ".bartrack", ".barfill", ".smalls",
                     ".tile.clean"):
        assert f"{selector} " not in css and f"{selector}{{" not in css, (
            f"app.css still defines {selector}")


def test_the_shell_template_hardcodes_no_product_link():
    base = (CORE_SRC / "web" / "templates" / "_base.html").read_text(
        encoding="utf-8")
    for href in ('href="/environment"', 'href="/energy"'):
        assert href not in base, f"_base.html still hardcodes {href}"


def test_core_ships_no_product_template():
    names = {p.name for p in (CORE_SRC / "web" / "templates").glob("*.html")}
    assert "environment.html" not in names
    assert "energy.html" not in names


def test_the_shared_summary_reports_no_rooms():
    """
    The shared SUMMARY used to compute rooms and excursions, so an energy
    customer paid for a cleanroom query on every page they loaded.
    """
    from jetpace_core.web import queries
    for word in ("rooms_total", "rooms_in_spec", "excursions"):
        assert word not in queries.SUMMARY
