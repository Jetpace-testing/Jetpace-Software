"""
The supervisor: starts the application, keeps it running, and is the only
thing that may replace it.

Why this exists as a separate process rather than a few lines at the top of
the application: a frozen Python executable holds its own files open from
its first instruction, so it can no more replace itself than it can pull
itself up by its bootstraps. Meril RTD got away with doing the swap inside
`dist/main.js` because Node loads nothing until the first `import`. A
PyInstaller bundle has no such moment. So the swap moves out into a process
that never loads anything from `app/`.

    supervisor.exe                 this, frozen separately
    app/                           the application, frozen; replaceable
    version.json                   what app/ currently is
    update/                        see layout.py

The order on every start is fixed and matters:

    1. prepare()      apply a staged release, or roll back a failed one
    2. start children
    3. if an update is on trial, wait for the health gate
    4. confirm()      the update is over - or exit, and let the next start
                      count another attempt

Step 4 is what makes a bad release survivable. The supervisor does not
decide to roll back; it exits, the service manager restarts it, prepare()
counts the attempt, and after MAX_ATTEMPTS the backup goes back. Nothing
has to succeed for the rollback to happen, which is the property you want
when the thing that just failed is the software doing the deciding.

IMPORT CONSTRAINT. This module imports the standard library and
`jetpace_core.update` only. It must never import the poller, the web
application, psycopg, pymodbus or paho - test_supervisor.py enforces it.
Not for speed: the supervisor is frozen into its own executable with its
own copy of what it needs, so that nothing it has loaded lives in the
directory it is about to rename.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .update.swap import BootResult, confirm, prepare, read_pending

# How long a freshly swapped release has to become healthy. Generous: a
# cold PostgreSQL on a site PC can take twenty seconds to accept
# connections, and the first request builds a connection pool and renders
# a template. Too short here means a good release is rolled back, which is
# worse than a slow start.
HEALTH_DEADLINE = 120.0
HEALTH_EVERY = 2.0
HEALTH_TIMEOUT = 5.0

# Restarting a child that keeps dying, without spinning. Capped rather than
# unbounded: a site that has been broken for an hour should still be trying.
BACKOFF = (5.0, 10.0, 30.0, 60.0)

# How long a child gets to exit on its own after being asked.
STOP_GRACE = 10.0


# --- keeping children from outliving us -----------------------------------
#
# A killed process on Windows does NOT take its children with it. The
# supervisor's own stop_all() handles an orderly shutdown, but a hard kill -
# Task Manager, `taskkill /F`, a service crash - leaves the poller, ingest
# and web processes running and holding their ports.
#
# Measured, not assumed: killing the supervisor left all four children alive
# with 1886 and 5020 still bound. The consequence is worse than untidy. On
# the next start the new web child cannot bind the port, while the ORPHAN
# keeps answering /healthz with ok:true - so a release on trial passes a
# health gate it never earned, and a broken version is confirmed as good.
#
# A job object with KILL_ON_JOB_CLOSE is the one mechanism Windows has for
# this. The supervisor holds the only handle; when the process ends, however
# it ends, the handle closes and everything in the job is terminated.
#
# This is the exact mirror of CREATE_BREAKAWAY_FROM_JOB in
# scripts/setup_postgres.py, where the database must survive the shell that
# started it. Same mechanism, opposite requirement.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def create_kill_on_close_job() -> int | None:
    """
    A job whose members are terminated when the last handle to it closes.
    Returns the handle, or None where that is not available - in which case
    the supervisor still works and children can simply outlive a hard kill.
    """
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = \
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            ctypes.c_void_p(job), _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            k32.CloseHandle(ctypes.c_void_p(job))
            return None
        return job
    except (OSError, AttributeError):
        return None


def assign_to_job(job: int | None, pid: int) -> bool:
    """
    Put a process in the job. The handle is opened fresh rather than taken
    from Popen._handle, which is private and would tie this to an
    implementation detail of subprocess.
    """
    if job is None or os.name != "nt":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                                 False, pid)
        if not handle:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(ctypes.c_void_p(job),
                                                     ctypes.c_void_p(handle)))
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    except (OSError, AttributeError):
        return False


@dataclass
class ChildSpec:
    name: str
    args: list[str]
    cwd: Path | None = None
    # A required child's death is a failure of the release. The simulator,
    # for instance, is not required.
    required: bool = True


@dataclass
class Child:
    spec: ChildSpec
    process: subprocess.Popen | None = None
    starts: int = 0
    next_start: float = 0.0
    log: Path | None = None
    _handle: object | None = field(default=None, repr=False)


class Supervisor:
    def __init__(
        self,
        root: str | Path,
        children: list[ChildSpec],
        health_url: str,
        logs: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.children = [Child(spec=s) for s in children]
        self.health_url = health_url
        self.logs = Path(logs) if logs else self.root / "logs"
        self.stopping = threading.Event()
        self._boot: BootResult | None = None
        # Held for the supervisor's whole life. Closing it - which happens
        # when this process ends, however it ends - kills every child.
        self._job = create_kill_on_close_job()

    # --- logging ----------------------------------------------------------
    # Deliberately not the logging module: this runs before the application
    # exists and anything cleverer is one more thing that can fail first.
    #
    # It must write to a FILE as well as stdout. Under the service control
    # manager there is no console, so print() goes nowhere - which is how
    # the first service deployment ended up with logs for all four children
    # and none for the supervisor itself. The supervisor's messages are the
    # ones that matter most: what prepare() did, why a release was rolled
    # back, which child keeps dying.

    def say(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  supervisor  {msg}"
        print(line, flush=True)
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            with (self.logs / "supervisor.log").open(
                    "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass        # a supervisor that cannot log must still supervise

    # --- version ----------------------------------------------------------

    def current_version(self) -> str:
        path = self.root / "version.json"
        try:
            version = json.loads(path.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError, TypeError):
            return "0.0.0"
        # A null or numeric version must not become the string "None" or
        # "1.4", either of which would then be compared against real
        # versions and quietly lose.
        return version if isinstance(version, str) and version else "0.0.0"

    # --- children ---------------------------------------------------------

    def _spawn(self, child: Child) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        child.log = self.logs / f"{child.spec.name}.log"
        handle = child.log.open("a", encoding="utf-8", buffering=1)
        child._handle = handle
        handle.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")

        # DETACHED_PROCESS is deliberately NOT used: these children must die
        # with the supervisor, unlike the database, which must not. stdout
        # goes to a file rather than a pipe because nobody reads the pipe and
        # a full pipe buffer blocks the child forever - the same failure that
        # made pg_ctl hang when its output was captured.
        child.process = subprocess.Popen(
            child.spec.args,
            cwd=str(child.spec.cwd or self.root),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        child.starts += 1
        in_job = assign_to_job(self._job, child.process.pid)
        self.say(f"started {child.spec.name} (pid {child.process.pid}, "
                 f"start {child.starts}) -> {child.log.name}"
                 f"{'' if in_job else '  [not in job]'}")

    def _stop(self, child: Child) -> None:
        p = child.process
        if p is None or p.poll() is not None:
            self._close(child)
            return
        self.say(f"stopping {child.spec.name} (pid {p.pid})")
        try:
            p.terminate()
            p.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            self.say(f"{child.spec.name} did not stop; killing it")
            p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.say(f"{child.spec.name} will not die; carrying on")
        except OSError as exc:
            self.say(f"could not stop {child.spec.name}: {exc}")
        self._close(child)

    def _close(self, child: Child) -> None:
        handle = child._handle
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            child._handle = None

    def stop_all(self) -> None:
        for child in reversed(self.children):     # web first, poller last
            self._stop(child)

    def _backoff_for(self, starts: int) -> float:
        return BACKOFF[min(starts, len(BACKOFF)) - 1] if starts else 0.0

    # --- health -----------------------------------------------------------

    def healthy(self) -> tuple[bool, str]:
        """
        One honest question: does the application answer, and does it say it
        is well? A listening socket is not enough - that was the lesson from
        registering PostgreSQL, where the port opens before the database
        will accept a connection.
        """
        try:
            with urllib.request.urlopen(self.health_url,
                                        timeout=HEALTH_TIMEOUT) as r:
                if r.status != 200:
                    return False, f"HTTP {r.status}"
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"not answering ({exc.reason if hasattr(exc, 'reason') else exc})"
        except ValueError:
            return False, "answered with something that is not JSON"
        if body.get("ok") is not True:
            return False, f"says it is not well: {body}"
        return True, "ok"

    def wait_for_health(self, deadline: float | None = None) -> tuple[bool, str]:
        # Read the module constant at call time rather than binding it as a
        # default argument. A default is captured once at import, which makes
        # the value impossible to change afterwards - including from a test,
        # which is how this was noticed.
        deadline = HEALTH_DEADLINE if deadline is None else deadline
        give_up = time.monotonic() + deadline
        last = "never answered"
        while time.monotonic() < give_up and not self.stopping.is_set():
            # A required child dying during the gate is a failure now; there
            # is no point waiting out the deadline for something that has
            # already gone.
            for child in self.children:
                if (child.spec.required and child.process is not None
                        and child.process.poll() is not None):
                    return False, (f"{child.spec.name} exited with code "
                                   f"{child.process.returncode}")
            ok, why = self.healthy()
            if ok:
                return True, why
            last = why
            self.stopping.wait(HEALTH_EVERY)
        return False, last

    # --- the run loop -----------------------------------------------------

    def run(self) -> int:
        # 1. Before anything else, and before any child exists.
        boot = prepare(self.root)
        self._boot = boot
        version = self.current_version()
        self.say(f"starting, version {version}")
        if boot:
            self.say(boot.describe())
        if boot.error:
            self.say(f"UPDATE PROBLEM: {boot.error}")

        self._install_signal_handlers()

        for child in self.children:
            self._spawn(child)

        # 2. If an update is on trial, it has to prove itself.
        on_trial = read_pending(self.root)
        if on_trial is not None and not on_trial.staged:
            self.say(f"release {on_trial.to_version} is on trial "
                     f"(attempt {on_trial.attempts} of 3); waiting for "
                     f"{self.health_url}")
            ok, why = self.wait_for_health()
            if self.stopping.is_set():
                self.stop_all()
                return 0
            if not ok:
                # Do not attempt the rollback here. Exit, and let the next
                # start count the attempt - that path works even if this
                # process is the thing that is broken.
                self.say(f"HEALTH GATE FAILED: {why}")
                self.say("exiting so the next start counts another attempt")
                self.stop_all()
                return 1
            done = confirm(self.root)
            if done is not None:
                self.say(f"release {done.to_version} passed its health gate; "
                         f"the update is complete")

        # 3. Ordinary supervision.
        return self._supervise()

    def _supervise(self) -> int:
        while not self.stopping.is_set():
            now = time.monotonic()
            for child in self.children:
                p = child.process
                if p is None:
                    if now >= child.next_start:
                        self._spawn(child)
                    continue
                code = p.poll()
                if code is None:
                    continue
                self._close(child)
                wait = self._backoff_for(child.starts)
                self.say(f"{child.spec.name} exited with code {code}; "
                         f"restarting in {wait:.0f}s")
                child.process = None
                child.next_start = now + wait
            self.stopping.wait(1.0)

        self.say("stopping")
        self.stop_all()
        self.say("stopped")
        return 0

    # --- configuration ----------------------------------------------------

    @staticmethod
    def from_config(path: str | Path) -> "Supervisor":
        """
        Build a Supervisor from a JSON file.

        A Windows service cannot be handed command-line arguments the way a
        shell can, so what it runs has to be written down somewhere. A file
        in the install root is the version a person can read: whoever is
        standing at the PC can see exactly what the service will start,
        without the registry or `sc qc`.

        scripts/run_supervisor.py reads the same file, so the development
        run and the service run cannot drift apart.
        """
        path = Path(path)
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not read {path}: {exc}") from exc

        for field in ("root", "health_url", "children"):
            if field not in config:
                raise ValueError(f"{path} has no {field!r}")

        children = []
        for raw in config["children"]:
            if not raw.get("name") or not raw.get("args"):
                raise ValueError(f"{path} has a child with no name or args")
            children.append(ChildSpec(
                name=str(raw["name"]),
                args=[str(a) for a in raw["args"]],
                cwd=Path(raw["cwd"]) if raw.get("cwd") else None,
                required=bool(raw.get("required", True)),
            ))
        if not children:
            raise ValueError(f"{path} lists no children")

        return Supervisor(
            root=config["root"],
            children=children,
            health_url=str(config["health_url"]),
            logs=config.get("logs"),
        )

    # --- signals ----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            self.say(f"signal {signum}; shutting down")
            self.stopping.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass          # not the main thread, or not supported
