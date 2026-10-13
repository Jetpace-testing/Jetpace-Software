"""
Where things are, in both layouts.

Path resolution is the one thing a source-checkout test suite cannot
really prove, because the bug it guards against only exists once frozen:
inside a PyInstaller bundle `__file__` points into _internal, so an
install root derived from it lands in the directory that updates replace.

So these tests fake frozen-ness rather than asserting the real thing, and
the real thing is checked by `jetpace where` during the build - see
smoke_test() in scripts/build.py. Both matter: these pin the rules, the
build pins reality.
"""

import os
import sys
from pathlib import Path

import pytest

from jetpace_core import paths


@pytest.fixture
def unfrozen(monkeypatch):
    monkeypatch.delenv("JETPACE_ROOT", raising=False)
    monkeypatch.setattr(paths, "is_frozen", lambda: False)


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """A fake frozen install: <root>/app/jetpace.exe"""
    app = tmp_path / "app"
    app.mkdir()
    exe = app / "jetpace.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.delenv("JETPACE_ROOT", raising=False)
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", str(exe))
    return tmp_path


# --- the install root -----------------------------------------------------

def test_a_checkout_resolves_the_repository_root(unfrozen):
    root = paths.install_root()
    assert (root / "packages" / "core").is_dir()
    assert (root / "scripts").is_dir()


def test_a_frozen_install_resolves_two_up_from_the_executable(frozen):
    """
    <root>/app/jetpace.exe -> <root>. NOT sys._MEIPASS, which for --onedir
    is the _internal folder: using that would put the database inside the
    directory an update replaces.
    """
    assert paths.install_root() == frozen.resolve()


def test_jetpace_root_overrides_everything(frozen, tmp_path, monkeypatch):
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.setenv("JETPACE_ROOT", str(elsewhere))
    assert paths.install_root() == elsewhere.resolve()


# --- the data directory ---------------------------------------------------

def test_a_checkout_uses_dot_local(unfrozen):
    assert paths.data_dir().name == ".local"


def test_a_fresh_frozen_install_uses_data(frozen):
    assert paths.data_dir() == frozen.resolve() / "data"


def test_whichever_directory_exists_wins(frozen):
    """
    So a frozen executable can be pointed at a source checkout with
    JETPACE_ROOT and find its .local - which is how the frozen build gets
    tested against a real database before an installer exists.
    """
    (frozen / ".local").mkdir()
    assert paths.data_dir() == frozen.resolve() / ".local"


def test_data_wins_over_dot_local_when_both_exist(frozen):
    (frozen / ".local").mkdir()
    (frozen / "data").mkdir()
    assert paths.data_dir() == frozen.resolve() / "data"


def test_the_data_directory_is_never_inside_app(frozen):
    """
    An update replaces app/ wholesale. A customer's readings, passwords
    and logs inside it would be deleted by the first successful update -
    silently, and only noticed afterwards.
    """
    assert "app" not in paths.data_dir().relative_to(frozen.resolve()).parts


# --- secrets --------------------------------------------------------------

def test_a_missing_secret_says_what_to_do(frozen):
    (frozen / "data").mkdir()
    with pytest.raises(SystemExit) as exc:
        paths.secret("app_password")
    message = str(exc.value)
    assert "app_password" in message
    assert "setup_postgres" in message


def test_a_secret_is_read_and_stripped(frozen):
    (frozen / "data").mkdir()
    (frozen / "data" / "app_password").write_text("  hunter2\n")
    assert paths.secret("app_password") == "hunter2"


def test_the_dsn_carries_the_password_but_the_test_does_not_assert_it(frozen):
    (frozen / "data").mkdir()
    (frozen / "data" / "app_password").write_text("s3cret")
    dsn = paths.postgres_dsn()
    assert dsn.startswith("postgresql://jetpace_app:")
    assert "@127.0.0.1:5433/jetpace" in dsn


def test_broker_credentials_are_none_without_a_password_file(frozen):
    """
    A broker with no authentication is a valid development case; refusing
    to start would be unhelpful.
    """
    (frozen / "data").mkdir()
    assert paths.broker_credentials() == (None, None)


def test_broker_credentials_come_from_the_file(frozen):
    (frozen / "data").mkdir()
    (frozen / "data" / "mqtt_password").write_text("brokerpw\n")
    user, password = paths.broker_credentials()
    assert user == "jetpace_app"
    assert password == "brokerpw"


# --- version --------------------------------------------------------------

def test_the_version_comes_from_version_json(frozen):
    (frozen / "version.json").write_text('{"version": "1.4.2"}')
    assert paths.version() == "1.4.2"


@pytest.mark.parametrize("content", ["", "{}", "not json",
                                     '{"version": null}', '{"version": 1.4}'])
def test_a_broken_version_file_falls_back(frozen, content):
    (frozen / "version.json").write_text(content)
    assert paths.version() == "0.0.0"


def test_no_version_file_falls_back(frozen):
    assert paths.version() == "0.0.0"
