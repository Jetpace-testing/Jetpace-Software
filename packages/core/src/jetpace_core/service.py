"""
The supervisor as a Windows service.

Two reasons this exists rather than a shortcut:

  Something has to start the supervisor at boot, before anyone signs in.
  A site PC reboots for Windows Update at 3am and nobody is there.

  Something has to restart the supervisor when it exits. That is not a
  nicety - it is the mechanism that makes a bad release recoverable. The
  supervisor does not roll back a failed update itself; it exits, and the
  service manager starting it again is what counts the attempt. After three,
  swap.prepare() puts the backup back. Without a service manager underneath
  it, a release that will not start simply stays broken.

So the exit code matters. pywin32's own documentation is the rule here:

    If this method raises an exception, the C framework will detect this
    and report a SERVICE_STOPPED status with a non-zero error code.

A non-zero error code is what makes the SCM treat the stop as a failure and
fire the actions set by `sc failure`. A clean exit does not. That is why
SvcDoRun raises rather than returning quietly when the health gate fails -
returning would look like "the administrator stopped it", and nothing would
restart it.

This module is not the supervisor. It imports pywin32, which the supervisor
must not, and it does nothing except translate between the SCM and
Supervisor.run().
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

from .supervisor import Supervisor

SERVICE_NAME = "jetpace-supervisor"
DISPLAY_NAME = "Jetpace Supervisor"
DESCRIPTION = (
    "Runs the Jetpace acquisition, ingest and dashboard processes, keeps "
    "them running, and applies signed software updates."
)

# Where the service looks for what to run. Set when the service is
# registered; read on every start.
CONFIG_ENV = "JETPACE_SUPERVISOR_CONFIG"


class HealthGateFailed(Exception):
    """
    The release on trial did not come up healthy.

    Raised so the SCM sees a non-zero exit and restarts us, which is what
    counts the attempt that eventually triggers the rollback. It is the
    intended path, not a crash - the message says so for whoever reads the
    event log.
    """


class JetpaceSupervisorService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args) -> None:
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.supervisor: Supervisor | None = None

    # --- the SCM asks us to stop ------------------------------------------

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING,
                                 waitHint=30_000)
        if self.supervisor is not None:
            # The supervisor stops its children in reverse order and waits
            # for them, which is why the wait hint above is generous.
            self.supervisor.stopping.set()
        win32event.SetEvent(self.stop_event)

    # --- the SCM starts us ------------------------------------------------

    def SvcDoRun(self) -> None:
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""))

        config = os.environ.get(CONFIG_ENV) or ""
        if not config:
            # Registered without a config path. Say so in the event log
            # rather than failing silently, and raise so this is not
            # mistaken for a clean stop.
            self._log_error(
                f"{CONFIG_ENV} is not set, so the service does not know what "
                f"to run. Re-register with "
                f"scripts/register_supervisor_service.py")
            raise RuntimeError(f"{CONFIG_ENV} is not set")

        try:
            self.supervisor = Supervisor.from_config(Path(config))
        except ValueError as exc:
            self._log_error(f"the supervisor configuration is unusable: {exc}")
            raise

        try:
            code = self.supervisor.run()
        except Exception:                                   # noqa: BLE001
            self._log_error("the supervisor stopped with an error:\n"
                            + traceback.format_exc())
            raise

        if code != 0:
            # The one case worth spelling out: a release failed its health
            # gate. Raising makes the SCM restart us, which is the whole
            # rollback mechanism.
            self._log_error(
                "a release did not pass its health gate, so this service is "
                "exiting on purpose. Windows will start it again; after three "
                "attempts the previous version is restored automatically.")
            raise HealthGateFailed(f"supervisor exited with {code}")

    # --- the event log ----------------------------------------------------

    def _log_error(self, message: str) -> None:
        try:
            servicemanager.LogErrorMsg(f"{SERVICE_NAME}: {message}")
        except Exception:                                   # noqa: BLE001
            pass        # a service that cannot log must still behave


def main() -> None:
    """
    Entry point. With no arguments the SCM is starting us; with arguments
    it is a person running install/remove/start/stop by hand.
    """
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(JetpaceSupervisorService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(JetpaceSupervisorService)


if __name__ == "__main__":
    main()
