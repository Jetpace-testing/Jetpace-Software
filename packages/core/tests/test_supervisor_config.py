"""
The supervisor's configuration file.

A Windows service cannot be handed command-line arguments the way a shell
can, so what the supervisor runs is written to a file that both the service
and scripts/run_supervisor.py read. That makes the file a contract, and a
malformed one must fail with a sentence rather than a traceback - it is
read before anything can log.
"""

import ast
import json
from pathlib import Path

import pytest

from jetpace_core.supervisor import Supervisor


def write(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "supervisor.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


GOOD = {
    "root": "D:/Jetpace",
    "health_url": "http://127.0.0.1:1885/healthz",
    "logs": "D:/Jetpace/.local/logs",
    "children": [
        {"name": "poller", "args": ["py", "run_poller.py", "--sink", "mqtt"]},
        {"name": "web", "args": ["py", "run_web.py"]},
    ],
}


def test_a_good_config_builds_a_supervisor(tmp_path):
    s = Supervisor.from_config(write(tmp_path, GOOD))
    assert [c.spec.name for c in s.children] == ["poller", "web"]
    assert s.health_url == "http://127.0.0.1:1885/healthz"
    assert s.children[0].spec.args[-1] == "mqtt"


def test_children_are_required_unless_they_say_otherwise(tmp_path):
    config = {**GOOD, "children": [
        {"name": "poller", "args": ["py"]},
        {"name": "simulator", "args": ["py"], "required": False},
    ]}
    s = Supervisor.from_config(write(tmp_path, config))
    assert s.children[0].spec.required is True
    assert s.children[1].spec.required is False


def test_a_child_can_name_its_own_working_directory(tmp_path):
    config = {**GOOD, "children": [
        {"name": "poller", "args": ["py"], "cwd": "D:/elsewhere"},
    ]}
    s = Supervisor.from_config(write(tmp_path, config))
    assert s.children[0].spec.cwd == Path("D:/elsewhere")


def test_logs_default_to_the_install_root_when_not_given(tmp_path):
    config = {k: v for k, v in GOOD.items() if k != "logs"}
    s = Supervisor.from_config(write(tmp_path, config))
    assert s.logs == Path("D:/Jetpace").resolve() / "logs"


@pytest.mark.parametrize("missing", ["root", "health_url", "children"])
def test_a_missing_field_says_which(tmp_path, missing):
    config = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        Supervisor.from_config(write(tmp_path, config))


def test_an_empty_child_list_is_refused(tmp_path):
    """
    A supervisor with nothing to supervise would start, report healthy and
    run no software at all - the worst kind of failure, because it looks
    like success.
    """
    with pytest.raises(ValueError, match="no children"):
        Supervisor.from_config(write(tmp_path, {**GOOD, "children": []}))


@pytest.mark.parametrize("child", [
    {"args": ["py"]},                      # no name
    {"name": "poller"},                    # no args
    {"name": "", "args": ["py"]},
    {"name": "poller", "args": []},
])
def test_a_child_with_no_name_or_no_args_is_refused(tmp_path, child):
    with pytest.raises(ValueError, match="no name or args"):
        Supervisor.from_config(write(tmp_path, {**GOOD, "children": [child]}))


def test_a_file_that_is_not_json_says_so(tmp_path):
    path = tmp_path / "supervisor.json"
    path.write_text("this is not json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not read"):
        Supervisor.from_config(path)


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(ValueError, match="could not read"):
        Supervisor.from_config(tmp_path / "nope.json")


# --- the service wrapper's import surface ---------------------------------

def test_the_service_wrapper_imports_no_application_code():
    """
    service.py may import pywin32, which supervisor.py may not. What it
    must still not import is anything that lives in app/ - the folder the
    supervisor has to rename.
    """
    import jetpace_core.service as svc

    tree = ast.parse(Path(svc.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))

    forbidden = {"psycopg", "pymodbus", "paho", "fastapi", "uvicorn", "jinja2"}
    offenders = {n for n in names for bad in forbidden
                 if n == bad or n.startswith(bad + ".")}
    assert not offenders, f"service.py imports {offenders}"

    ours = {n for n in names
            if n.startswith(".") or n.startswith("jetpace_core")}
    assert ours == {".supervisor"}, f"unexpected internal imports: {ours}"


def test_a_non_zero_supervisor_exit_is_raised_not_returned():
    """
    The rollback depends on this. pywin32 reports a non-zero error code to
    the SCM only if SvcDoRun raises; returning quietly looks like "the
    administrator stopped it", and nothing would restart the service - so
    the attempt would never be counted and a bad release would stay.
    """
    import jetpace_core.service as svc

    source = Path(svc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "SvcDoRun")

    raises = [n for n in ast.walk(run_method) if isinstance(n, ast.Raise)]
    assert raises, "SvcDoRun must raise on failure, not return"
    assert "HealthGateFailed" in source
