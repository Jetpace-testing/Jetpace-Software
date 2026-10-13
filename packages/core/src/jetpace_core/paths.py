"""
Where things are, whether this is a source checkout or a frozen install.

Every entry point used to work out the install root itself, with

    ROOT = Path(__file__).resolve().parent.parent

which is right in a checkout and wrong in a frozen build: `__file__` then
points inside the PyInstaller bundle, so `.local/app_password` resolves to
a directory that does not exist and the process dies looking for a
password that is sitting two folders away. Four copies of that line meant
four chances to get it wrong differently.

THE TWO LAYOUTS

A source checkout:

    D:\\Jetpace EMS\\                 <- the root
      packages/core/src/jetpace_core/
      scripts/
      config/prototype.toml
      .local/                        secrets, data, logs

A frozen install:

    C:\\Program Files\\Jetpace\\       <- the root
      app\\jetpace.exe                this executable
      app\\_internal\\                its bundled libraries
      supervisor\\supervisor.exe
      version.json
      config\\prototype.toml
      data\\                          secrets, database, logs

The root is what both have in common, and everything else is named
relative to it. `.local` in a checkout and `data` in an install are the
same thing under different names - `.local` is gitignored, which is why
it is called that, and a customer's PC has no git to ignore anything.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def install_root() -> Path:
    """
    The directory everything else hangs off.

    JETPACE_ROOT wins when set, which is what lets a test or a second
    install on one machine point somewhere else without arguments.
    """
    override = os.environ.get("JETPACE_ROOT")
    if override:
        return Path(override).resolve()

    if is_frozen():
        # sys.executable is <root>\app\jetpace.exe, so the root is two up.
        # Not sys._MEIPASS: for --onedir that is the _internal folder, and
        # using it would put the database inside the thing updates replace.
        return Path(sys.executable).resolve().parent.parent

    # A marker file, not a parent count.
    #
    # This used to be `parents[4]`, with a comment counting the levels. It
    # was right, but it is right only for a file at exactly this depth, and
    # web/app.py had made the same mistake one level out - it counted four
    # where it needed five, and the only symptom was the dashboard
    # refusing to start with "No database". As packages/ grows a module
    # three directories deep gets a different number, and nothing says so
    # until something cannot find its password.
    #
    # uv.lock specifically, and not pyproject.toml: there is a
    # pyproject.toml in every workspace member, so a walk looking for that
    # would stop at packages/core and call it the root. The lock file
    # exists once, at the workspace root, which is the thing being looked
    # for.
    here = Path(__file__).resolve()
    for directory in here.parents:
        if (directory / "uv.lock").exists():
            return directory

    # No lock file anywhere above: not a workspace checkout, so fall back
    # to the depth this file actually sits at.
    return here.parents[4]


def data_dir() -> Path:
    """
    Secrets, the database, logs. Never inside app/, because an update
    replaces app/ wholesale and a customer's readings must not be in it.
    """
    root = install_root()
    # Whichever exists wins, so a frozen executable can be pointed at a
    # source checkout with JETPACE_ROOT and find its .local - which is how
    # the frozen build gets tested against a real database before there is
    # an installer to make a real install with.
    for name in ("data", ".local"):
        if (root / name).is_dir():
            return root / name
    # Neither is there yet. An install creates data/; a checkout .local/.
    return root / ("data" if is_frozen() else ".local")


def config_dir() -> Path:
    return install_root() / "config"


def _either(installed: str, *checkout: str) -> Path:
    """
    A payload directory, named differently in the two layouts.

    An installer lays PostgreSQL down at <root>\\pgsql. A checkout keeps it
    at installer\\vendor\\pgsql, because there it is a build artefact that
    is fetched and pinned rather than something shipped. Whichever exists
    wins; the installed name is the fallback, since that is the layout a
    customer has.
    """
    root = install_root()
    for candidate in (root.joinpath(installed), root.joinpath(*checkout)):
        if candidate.is_dir():
            return candidate
    return root / installed


def pgsql_dir() -> Path:
    """PostgreSQL and TimescaleDB binaries."""
    return _either("pgsql", "installer", "vendor", "pgsql")


def mosquitto_dir() -> Path:
    """The broker binaries."""
    return _either("mosquitto", "installer", "vendor", "mosquitto")


def migrations_dir() -> Path:
    return install_root() / "migrations"


def logs_dir() -> Path:
    return data_dir() / "logs"


def secret(name: str) -> str:
    """
    Read a generated credential, with a message that says what to do when
    it is not there. The alternative - a bare FileNotFoundError from inside
    a connection helper - is a stack trace that names the wrong problem.
    """
    path = data_dir() / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise SystemExit(
            f"{path} is missing, so there is no {name} to connect with.\n"
            f"  A source checkout generates it with "
            f"scripts/setup_postgres.py;\n"
            f"  an install generates it at install time, so this one is "
            f"incomplete.") from None
    except PermissionError:
        raise SystemExit(
            f"{path} cannot be read by {os.environ.get('USERNAME', 'this '
            'account')}.\n"
            f"  Secret files are restricted to SYSTEM, Administrators and "
            f"the installing user.") from None


def postgres_dsn(user: str = "jetpace_app", database: str = "jetpace",
                 host: str = "127.0.0.1", port: int = 5433) -> str:
    return (f"postgresql://{user}:{secret('app_password')}"
            f"@{host}:{port}/{database}")


def broker_credentials() -> tuple[str | None, str | None]:
    """
    The broker user and password, or (None, None) where no password file
    exists - a broker configured without authentication is a valid
    development case, and refusing to start would be unhelpful.
    """
    path = data_dir() / "mqtt_password"
    if not path.exists():
        return None, None
    return "jetpace_app", path.read_text(encoding="utf-8").strip()


def version() -> str:
    """What version.json says, or 0.0.0 in a checkout that has none."""
    import json
    try:
        v = json.loads(
            (install_root() / "version.json").read_text(encoding="utf-8"))
        return v["version"] if isinstance(v.get("version"), str) else "0.0.0"
    except (OSError, ValueError, KeyError, TypeError):
        return "0.0.0"
