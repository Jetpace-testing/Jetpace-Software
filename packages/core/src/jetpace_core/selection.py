"""
Which products this install serves.

One question, asked by three processes for three different reasons, which
is why it is here rather than in `web/products.py` where it started.

  the WEB process  which routes to mount, which templates to search,
                   which tabs to draw.
  the POLLER       which register maps exist, and therefore which devices
                   in the database can be read at all.
  the SERVICE
  REGISTRATION     how many dashboard children to write into
                   supervisor.json, and on which ports.

Only the first is a web concern. `web/products.py` imports FastAPI and
Jinja at module scope, so asking it would mean the poller loading a web
framework to find out where a register lives - and would invert the
dependency
direction architecture.md §3 sets out, where products and the web layer
sit above acquisition rather than beside it. `web/products.py` imports
these three names and re-exports them, so it stays the single place a
reader looks for the seam.

Nothing here imports a product, or anything of ours beyond `paths`.
Resolving a slug to a package is the next step up and belongs with the
thing that knows what a Product is.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import paths

# Used only when configuration does not say. These are strings, not
# imports: core still names no product at module scope, and a slug here
# that has no package is simply skipped by the callers that fall back to
# it.
#
# Order is the order they appear in the navigation.
KNOWN: tuple[str, ...] = ("environment", "energy")

CONFIG_NAME = "products.toml"


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
