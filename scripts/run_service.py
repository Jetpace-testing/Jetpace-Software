"""
The service host. What the SCM actually launches.

    "<venv>\\Scripts\\python.exe" "<repo>\\scripts\\run_service.py"

Started with no arguments, this hands control to the service dispatcher and
the SCM takes over. Run with arguments it behaves like any pywin32 service
script - install, remove, start, stop, debug.

WHY NOT pythonservice.exe, WHICH IS THE USUAL ANSWER

Two reasons, both found by trying it:

  pythonservice.exe finds the class to host in the registry, as the DEFAULT
  value of a PythonClass SUBKEY. Get that wrong and the service starts,
  fails to find the class, and dies before it can log anything - a STOPPED
  service and an empty event log. Naming the class in a script instead
  removes a registry convention from the picture entirely.

  pythonservice.exe lives in the virtual environment, but the interpreter
  DLL does not: uv keeps python312.dll in its own store under the user's
  profile, and that directory is not on the system PATH. A service gets
  the SYSTEM environment, not the installing user's, so it could not load
  the DLL. The venv's own python.exe has no such problem - it resolves its
  interpreter through pyvenv.cfg.

This is also closer to where this is going. A frozen build hosts the
service from its own executable, with no interpreter to locate and no
registry entry to get right.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

from jetpace_core.service import main                           # noqa: E402

if __name__ == "__main__":
    main()
