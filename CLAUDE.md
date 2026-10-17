# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Read this first

**[architecture.md](architecture.md)** holds the module boundaries, the
invariants, the data model and the honest list of what is built versus
specified. Read it before changing code.

The platform specification — business case, deployment, licensing, GxP, delivery
plan — is **Revision E**:
<https://claude.ai/artifact/YULvyznhQxLwfUCGuajncP>. Read it with the Artifact
tool (`action: "read"`) before changing a design. Sections cited as §N refer to
it.

## What this is

Jetpace Software: one Python platform carrying two products — energy management
for 400+ Modbus meters, and environmental monitoring for cleanrooms. Software
only; the customer supplies the PC, the instruments and the converters. One
install per customer on their own Windows machine.

**Neither product currently carries an instrument.** `jetpace_environment` held
a differential pressure transmitter and the pages built around a pressure
cascade; all of it was removed on 17 October 2026, because the first real site
monitors temperature and relative humidity. The seam is intact and the
commissioning form is built — what is missing is a register map. See the
package docstring.

Three workspace packages: `jetpace-core` is the platform and the UI shell,
`jetpace-energy` and `jetpace-environment` are the products. **Core must never
import a product** — it reaches one only through `jetpace_core.web.products`,
which resolves a slug from `config/products.toml`. A test scans core's source
for violations. `architecture.md` §3 has the seam.

Successor to two systems that stay in production throughout: **Meril EMS**
(`D:\Meril EMS` — 236 meters, 15 gateways, TypeScript/Fastify/SQL Server, the
billing engine) and **Meril RTD** (the platform: roles, alarms, PDF reports, and
a working signed updater). Reuse both rather than rewriting: EMS is the more
complete domain, RTD the more complete platform.

## Commands

```bash
uv run pytest -q                                  # 753 tests; needs nothing running
uv run python scripts/run_web.py                  # dashboard on 127.0.0.1:1885
python scripts/register_service.py --status        # the PostgreSQL service
```

Full startup sequence in [architecture.md](architecture.md) §8.

## Conventions

- **Python 3.12**, `uv` workspace, threads not asyncio — the concurrency unit is
  the gateway, not the device, because a serial bus is sequential.
- FastAPI + Jinja + HTMX + ECharts. No JavaScript build step; every asset local,
  because the product must work offline.
- `pydantic` defines contracts between components.
- No port is a default. Database 5433, broker 1887, dashboard 1885.
- Secrets live in gitignored `.local/`. Never print a credential value — point at
  the file instead.
- PowerShell is the user's shell: `&&` does not work, and `sc` is an alias for
  `Set-Content`. Use `sc.exe` or `Restart-Service`.

## Invariants — do not change these

Full detail and rationale in [architecture.md](architecture.md) §7.

- Billing: the **last** reading closes a period; counters never run backwards; a
  blank always carries a status; never substitute a plausible guess.
- `reading` and the audit log are **append-only** for the application role.
- Modbus writes are disabled **at build time**, not by configuration. An
  instrument's slave address and baud rate are writable registers, and a device
  knocked off the bus from a browser is a site visit.
- Signed values are two's complement. Wire address = datasheet number − 40001.
- A register map lives in the product package that sells the instrument, never
  in core, and is pinned to its datasheet by a test in that package.
- A point's `scale` is checked against a real instrument before a site goes
  live. Many transmitters report tenths; read as whole units, every reading and
  every report is out by ten with nothing on screen to say so.
- Every table carries `tenant_id` and every query filters on it.
- Every component must permit commercial redistribution.

## Working notes

- **Nothing ships to a site until the updater works** (§12). It is the next build.
- Use the Write/Edit tools rather than shell heredocs for content containing
  escaped characters — heredocs here collapse `\n` into real newlines and corrupt
  f-strings.
- Tests passing is necessary, not sufficient. Running the prototype live found
  three faults no test caught. Run it before believing it.
