"""
Serve the dashboard.

    uv run python scripts/run_web.py
    uv run python scripts/run_web.py --port 1885 --host 0.0.0.0

Port 1885 matches Meril EMS, so bookmarks and alarm emails keep working
when a site is migrated. Binds loopback by default; pass --host 0.0.0.0 to
reach it from elsewhere on the plant LAN, which in production is paired
with a firewall rule on the private profile only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

import uvicorn                                              # noqa: E402
from jetpace_core.web.app import create_app, dsn_from_env    # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1885)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    app = create_app(dsn_from_env())
    print(f"\n  dashboard   http://{args.host}:{args.port}/")
    print(f"  health      http://{args.host}:{args.port}/healthz")
    print(f"  api docs    http://{args.host}:{args.port}/api/docs\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
