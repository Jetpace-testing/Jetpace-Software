"""
Run the supervisor over this working tree.

    uv run python scripts/run_supervisor.py
    uv run python scripts/run_supervisor.py --with-sim
    uv run python scripts/run_supervisor.py --root C:\\Jetpace

In development the children are the same scripts you would otherwise start
by hand in four terminals. On a site they are the frozen application in
app/, and the supervisor itself is a Windows service. The sequence is
identical either way, which is the point: the update path being exercised
here is the one that will run on a customer's PC.

The database and the broker are NOT children. They are long-lived services
that must outlive the supervisor - a restart to apply an update must not
take the historian down with it, and PostgreSQL in particular has to be
owned by the service manager so a shell cannot kill it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

from jetpace_core.supervisor import ChildSpec, Supervisor      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT,
                    help="install root; where update/ and version.json live")
    ap.add_argument("--port", type=int, default=1885)
    ap.add_argument("--with-sim", action="store_true",
                    help="also run the device simulator")
    ap.add_argument("--sim-units", type=int, default=3)
    ap.add_argument("--config", type=Path, default=ROOT / "supervisor.json",
                    help="use this config file if it exists")
    ap.add_argument("--no-config", action="store_true",
                    help="ignore supervisor.json and use the flags above")
    args = ap.parse_args()

    # Prefer the file the service reads, so running it by hand exercises
    # the same thing the service will. A drift between the two is exactly
    # the kind of difference that is found at a customer's site.
    if args.config.exists() and not args.no_config:
        print(f"using {args.config}")
        return Supervisor.from_config(args.config).run()

    py = sys.executable
    children: list[ChildSpec] = []

    if args.with_sim:
        # Not required: the simulator is a development convenience, and its
        # death must not fail a release's health gate.
        children.append(ChildSpec(
            name="simulator",
            args=[py, str(ROOT / "tools" / "sim" / "idp100_sim.py"),
                  "--units", str(args.sim_units)],
            required=False))

    children += [
        ChildSpec("poller", [py, str(ROOT / "scripts" / "run_poller.py"),
                             "--sink", "mqtt"]),
        ChildSpec("ingest", [py, str(ROOT / "scripts" / "run_ingest.py")]),
        ChildSpec("web", [py, str(ROOT / "scripts" / "run_web.py"),
                          "--port", str(args.port)]),
    ]

    supervisor = Supervisor(
        root=args.root,
        children=children,
        health_url=f"http://127.0.0.1:{args.port}/healthz",
        logs=ROOT / ".local" / "logs",
    )
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
