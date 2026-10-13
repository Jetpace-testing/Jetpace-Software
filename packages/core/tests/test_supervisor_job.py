"""
Children must not outlive the supervisor.

A killed process on Windows does not take its children with it. That was
measured rather than assumed: killing the supervisor left all four children
running with their ports still bound.

The consequence is worse than untidy. The orphaned web process keeps
answering /healthz with ok:true, so a release on trial passes a health gate
it never earned and a broken version is confirmed as good - the one failure
mode the gate exists to prevent.

These tests prove the job object actually terminates its members, by
spawning real processes and closing the handle.
"""

import ctypes
import os
import subprocess
import sys
import time

import pytest

from jetpace_core.supervisor import (
    ChildSpec,
    Supervisor,
    assign_to_job,
    create_kill_on_close_job,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows only")

SLEEPER = [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"]


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                         capture_output=True, text=True, timeout=30).stdout
    return str(pid) in out


def close(handle: int) -> None:
    ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))


# --- the mechanism --------------------------------------------------------

@windows_only
def test_a_job_can_be_created():
    job = create_kill_on_close_job()
    assert job is not None
    close(job)


@windows_only
def test_closing_the_job_kills_its_members():
    """
    The whole point. This is what a hard kill of the supervisor does: the
    handle closes and Windows terminates everything in the job.
    """
    job = create_kill_on_close_job()
    assert job is not None

    child = subprocess.Popen(SLEEPER, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        assert assign_to_job(job, child.pid) is True
        assert alive(child.pid)

        close(job)

        for _ in range(40):
            if child.poll() is not None:
                break
            time.sleep(0.25)
        assert child.poll() is not None, (
            "the child survived the job closing; a hard-killed supervisor "
            "would leave it holding its port")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@windows_only
def test_a_child_not_in_the_job_survives():
    """
    The control case. Without the job, a child outlives whatever started
    it - which is the bug being fixed, and worth pinning so the test above
    is known to be proving something.
    """
    child = subprocess.Popen(SLEEPER, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        job = create_kill_on_close_job()
        assert job is not None
        close(job)                      # closed without the child in it
        time.sleep(1.0)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=10)


@windows_only
def test_several_children_all_die_together():
    job = create_kill_on_close_job()
    children = [subprocess.Popen(SLEEPER, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                for _ in range(3)]
    try:
        for c in children:
            assert assign_to_job(job, c.pid)
        close(job)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all(c.poll() is not None for c in children):
                break
            time.sleep(0.25)
        assert all(c.poll() is not None for c in children)
    finally:
        for c in children:
            if c.poll() is None:
                c.kill()
                c.wait(timeout=10)


# --- how the supervisor uses it -------------------------------------------

@windows_only
def test_the_supervisor_creates_a_job(tmp_path):
    s = Supervisor(tmp_path, [], "http://127.0.0.1:1/healthz")
    assert s._job is not None
    close(s._job)


@windows_only
def test_a_spawned_child_is_put_in_the_job(tmp_path):
    s = Supervisor(tmp_path, [ChildSpec("sleeper", SLEEPER)],
                   "http://127.0.0.1:1/healthz", logs=tmp_path / "logs")
    try:
        s._spawn(s.children[0])
        pid = s.children[0].process.pid
        assert alive(pid)

        close(s._job)                   # as a hard kill of the supervisor would
        for _ in range(40):
            if s.children[0].process.poll() is not None:
                break
            time.sleep(0.25)
        assert s.children[0].process.poll() is not None
    finally:
        if s.children[0].process and s.children[0].process.poll() is None:
            s.children[0].process.kill()
        s._close(s.children[0])


def test_assigning_without_a_job_is_a_quiet_no(tmp_path):
    """
    Where a job cannot be created the supervisor must still work - children
    just outlive a hard kill, which is how it behaved before. Degrading is
    right here; refusing to start would be worse than the problem.
    """
    assert assign_to_job(None, os.getpid()) is False


@windows_only
def test_assigning_a_process_that_does_not_exist_is_a_quiet_no():
    job = create_kill_on_close_job()
    try:
        # A pid that cannot be opened. Reported, not raised: a child that
        # has already exited is an ordinary race, not an error.
        assert assign_to_job(job, 0x7FFFFFF0) is False
    finally:
        close(job)
