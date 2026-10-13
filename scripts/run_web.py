"""
Serve the dashboard.

    uv run python scripts/run_web.py [options]

A wrapper. The logic lives in jetpace_core.cli, because PyInstaller can
only freeze what is importable and scripts/ is composition rather than
library code. In a frozen install the same thing is reached as

    app\jetpace.exe web [options]

so what runs here is what runs at a site.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

from jetpace_core.cli import main                               # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["web", *sys.argv[1:]]))
