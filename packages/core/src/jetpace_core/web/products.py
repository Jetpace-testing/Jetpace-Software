"""
How a product gets its pages into the shell, without the shell knowing
which products exist.

THE PROBLEM. Core owns the shell: the base template, the overview, the
device pages, the chart furniture. The two products own their own views.
Dependencies point one way (architecture.md §3), so core must not import
`jetpace_energy` or `jetpace_environment` - but something has to put
their routes on the application, their templates where Jinja can find
them, and their names in the navigation.

THE SHAPE. Core defines `Product`, a description of what a product
contributes. Each product package exposes exactly one public function,
`jetpace_<slug>.web.product()`, returning one of these. `load()` resolves
slugs to packages by name at runtime, so the only place a product is
named is configuration.

WHY NOT ENTRY POINTS. The obvious Python answer is a `[project.entry-points]`
group and `importlib.metadata`. It does not survive freezing: `build.py`
passes no `--copy-metadata`, so the frozen bundle has no distribution
metadata to enumerate, and the application would start with no products
and no error. Importing by a name from config works identically frozen
and unfrozen, at the cost of `build.py` needing a `--hidden-import` per
product - which is a line in a build script rather than a runtime
mystery.

WHAT THIS COSTS. PyInstaller cannot see an `importlib.import_module` of a
computed name, so a product missing from `build.py`'s hidden imports is
absent from the bundle. `load()` therefore fails loudly, naming the
package and what to do - the one outcome that must not happen is a
dashboard that comes up healthy with its product pages quietly missing.
"""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.templating import Jinja2Templates

from .. import paths

# Used only when configuration does not say. These are strings, not
# imports: core still names no product at module scope, and a slug here
# that has no package is simply skipped by `load_default()`.
#
# Order is the order they appear in the navigation.
KNOWN: tuple[str, ...] = ("environment", "energy")

CONFIG_NAME = "products.toml"


@dataclass(frozen=True)
class Shell:
    """
    What core hands a product so that its pages belong to the same
    application as the rest.

    `templates` is the shared Jinja environment, already searching both
    core's template directory and every loaded product's, which is what
    lets a product template say `{% extends "_base.html" %}` and get
    core's shell. A product that built its own environment would render
    a page that slowly stopped looking like the product.
    """

    dsn: str
    templates: Jinja2Templates


@dataclass(frozen=True)
class Tile:
    """
    One figure on the overview, contributed by a product.

    The VALUE ARRIVES FORMATTED. A product knows that a differential
    pressure is one decimal place and reads in pascals, and that a meter
    reading is kWh with thousands separators; the shell does not and must
    not have to be told. Handing core a float and a format string would
    put the product's units back into the shared layer by the back door.

    `state` is "", "good" or "alarm", which the shell maps onto the same
    tile classes its own figures use - so a product's tile cannot invent
    a colour, and a cleanroom in spec is drawn exactly like any other
    healthy figure on the page.
    """

    label: str
    value: str
    small: str = ""
    state: str = ""

    def __post_init__(self) -> None:
        if self.state not in ("", "good", "alarm"):
            raise ValueError(
                f"tile {self.label!r} has state {self.state!r}; the shell "
                f"knows '', 'good' and 'alarm' and nothing else.")


@dataclass(frozen=True)
class Product:
    """One product's contribution to the application."""

    # Identity. `slug` is the URL ("/environment"), the nav key that
    # `{% block nav %}` matches, and the static mount path, so it has to
    # be a plain lowercase word.
    slug: str
    label: str

    # Where its templates live. Searched after core's, so a product
    # cannot shadow the shell.
    templates: Path

    # Called once with the Shell to build the product's routes. A
    # callable rather than a ready-made APIRouter because the routes need
    # the connection string and the shared template environment, and
    # neither exists at import time.
    routes: Callable[[Shell], APIRouter]

    # Optional: a directory of its own static files, and the stylesheet
    # within it that every page of this product needs. Served under
    # /static/<slug>/ and linked by the shell only when the product is
    # loaded, so a single-product install does not fetch the other
    # product's CSS.
    static: Path | None = None
    stylesheet: str | None = None

    # Optional: figures this product adds to the shared overview, given an
    # open connection. The overview is otherwise true of any product -
    # devices, how many are answering, how much has arrived - which is
    # correct and thin. A customer who bought one product wants their own
    # numbers on the first screen, and this is how they get there without
    # core learning what a room is.
    tiles: Callable[[Any], list[Tile]] | None = None

    def __post_init__(self) -> None:
        if not self.slug.isascii() or not self.slug.isalpha():
            raise ValueError(
                f"product slug {self.slug!r} must be a plain word: it is "
                f"used as a URL path and a static mount point.")
        if self.slug != self.slug.lower():
            raise ValueError(f"product slug {self.slug!r} must be lowercase.")
        if not self.templates.is_dir():
            raise ValueError(
                f"product {self.slug!r} names a template directory that does "
                f"not exist: {self.templates}. In a frozen build this means "
                f"build.py has no --add-data entry for it.")
        if self.stylesheet and self.static is None:
            raise ValueError(
                f"product {self.slug!r} names a stylesheet but no static "
                f"directory to serve it from.")
        if self.static is not None and not self.static.is_dir():
            raise ValueError(
                f"product {self.slug!r} names a static directory that does "
                f"not exist: {self.static}.")
        if self.stylesheet and not (self.static / self.stylesheet).is_file():
            raise ValueError(
                f"product {self.slug!r} names stylesheet "
                f"{self.stylesheet!r}, which is not in {self.static}.")


def _import(slug: str):
    """The one place a product package is named, and the name is data."""
    return importlib.import_module(f"jetpace_{slug}.web")


def load(slugs: Sequence[str]) -> list[Product]:
    """
    Resolve slugs to products, loudly.

    Every failure here is an install that cannot serve what it was asked
    to serve, so none of them is swallowed. A product that is configured
    and missing is a broken install, not a smaller one.
    """
    loaded: list[Product] = []
    for slug in slugs:
        try:
            module = _import(slug)
        except ImportError as exc:
            raise SystemExit(
                f"config/{CONFIG_NAME} enables the {slug!r} product, but "
                f"jetpace_{slug} could not be imported: {exc}\n"
                f"  In a checkout: check packages/{slug} exists and run "
                f"`uv sync`.\n"
                f"  In a frozen build: scripts/build.py needs "
                f"--hidden-import jetpace_{slug}.web") from None

        factory = getattr(module, "product", None)
        if not callable(factory):
            raise SystemExit(
                f"jetpace_{slug}.web has no product() function, so there is "
                f"nothing for the shell to mount.")

        product = factory()
        if not isinstance(product, Product):
            raise SystemExit(
                f"jetpace_{slug}.web.product() returned "
                f"{type(product).__name__}, not a Product.")
        if product.slug != slug:
            raise SystemExit(
                f"jetpace_{slug}.web.product() calls itself "
                f"{product.slug!r}. The slug is the URL and the nav key, so "
                f"it has to match the package it came from.")
        loaded.append(product)

    slugs_seen = [p.slug for p in loaded]
    if len(set(slugs_seen)) != len(slugs_seen):
        raise SystemExit(f"two products claim the same slug: {slugs_seen}")
    return loaded


def enabled(config_dir: Path | None = None) -> tuple[str, ...] | None:
    """
    Which products this install serves, from config/products.toml.

    Deliberately NOT read through cli.load_config(): that one returns the
    gateway list and exits when the file describes no gateways, which has
    nothing to do with whether the dashboard should show a tariff page.

    Deliberately NOT read from version.json either, tempting as it is -
    version.json is in update.layout.REPLACEABLE, so every OTA update
    overwrites it and an install's product selection would silently reset
    to whatever the release shipped. config/ is never touched by an
    update, which is exactly the property needed.

    Returns None - not a default list - when the install says nothing, so
    that "nobody configured this" and "somebody chose these" are different
    answers rather than one answer that has to be recognised by identity.
    A file that exists and is wrong is an error: somebody tried to say
    something.
    """
    path = (config_dir or paths.config_dir()) / CONFIG_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SystemExit(f"{path} cannot be read: {exc}") from None

    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{path} is not valid TOML: {exc}") from None

    if "enabled" not in cfg:
        return None

    names = cfg["enabled"]
    if not isinstance(names, list) or not all(
            isinstance(n, str) for n in names):
        raise SystemExit(
            f"{path}: `enabled` must be a list of product names, "
            f"for example enabled = [\"environment\"].")
    unknown = [n for n in names if not n.isascii() or not n.isalpha()]
    if unknown:
        raise SystemExit(
            f"{path}: {unknown} are not product names. Known products are "
            f"{list(KNOWN)}.")
    return tuple(names)


def load_default() -> list[Product]:
    """
    What `jetpace web` uses: configuration if there is any, otherwise
    whatever happens to be installed.

    The fallback skips a product that will not import, because an install
    that has not been told what it carries should serve what it has. A
    product named in configuration is never skipped - asking for a product
    that is not there is a broken install, and load() says so.
    """
    slugs = enabled()
    if slugs is None:
        return load([s for s in KNOWN if importable(s)])
    return load(slugs)


def importable(slug: str) -> bool:
    try:
        _import(slug)
    except ImportError:
        return False
    return True
