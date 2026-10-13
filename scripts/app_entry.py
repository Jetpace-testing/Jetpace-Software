"""
The frozen application's entry point. Not for running by hand.

PyInstaller freezes a SCRIPT, and a script runs as `__main__` with no
parent package - so pointing it at jetpace_core/cli.py directly produced a
34 MB executable that died on its first line:

    ImportError: attempted relative import with no known parent package

because `from . import paths` has no `.` to resolve. A launcher outside
the package fixes it: `jetpace_core` is importable, so the relative
imports inside it resolve normally.

In a source checkout use scripts/run_poller.py and friends, or
`python -m jetpace_core.cli`. This file exists for the build.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Only needed when run from a checkout; in a frozen bundle the package is
# already on the path and this directory does not exist.
_src = Path(__file__).resolve().parent.parent / "packages" / "core" / "src"
if _src.is_dir():
    sys.path.insert(0, str(_src))

from jetpace_core.cli import main                                # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
