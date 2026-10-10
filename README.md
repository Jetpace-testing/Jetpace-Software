# Jetpace Platform

Energy and environmental monitoring. Python 3.12, PostgreSQL + TimescaleDB,
MQTT on site. See the architecture specification for the full design; this
file is how to run what exists.

## Status

**Stage 2b complete.** Simulated devices through Modbus into TimescaleDB,
out through the rollups, and onto a dashboard. 39 tests passing.

| | |
|---|---|
| `tools/sim/idp100_sim.py` | Fake iDP-100 devices over Modbus TCP. Hand-written framing |
| `packages/core/.../decode.py` | Registers to numbers. 21 tests |
| `packages/core/.../registers.py` | Device maps, pinned to datasheets |
| `packages/core/.../poller.py` | Thread per gateway, offline backoff. 18 tests |
| `packages/core/.../modbus_client.py` | pymodbus wrapper; silence is not an exception |
| `packages/core/.../sinks.py` | Console and Postgres behind one Protocol |
| `migrations/0001_init.sql` | Tenancy, assets, the `reading` hypertable |
| `migrations/0002_rollups.sql` | Hourly and daily aggregates, `consumption()` |
| `migrations/0003_consumption_guard.sql` | `consumption()` refuses non-counters |
| `scripts/setup_postgres.py` | Private Postgres + Timescale. **Working** |
| `scripts/seed_prototype.py` | Config into the database |
| `scripts/run_poller.py` | Entry point |
| `scripts/seed_demo.py` | 30 days of realistic history, both product shapes |
| `packages/core/.../web/` | FastAPI + Jinja + HTMX + ECharts dashboard |
| `scripts/run_web.py` | Serves it on port 1885 |
| `docs/sql-practice.md` | 20 graded SQL exercises against this data |

Running locally right now: **PostgreSQL 17.11** with **TimescaleDB 2.30.2**
on `127.0.0.1:5433`, schema version 3.

## Run it

```bash
uv sync
```

Two terminals. First the devices:

```bash
uv run python tools/sim/idp100_sim.py --units 3
```

Then the poller:

```bash
uv run python scripts/run_poller.py
```

The dashboard:

```bash
uv run python scripts/run_web.py
```

Then <http://127.0.0.1:1885/>. Summary tiles, a live table that refreshes
itself, small multiples of pressure per room, the excursion report, and
`consumption()` with its real statuses.

Tests:

```bash
uv run pytest -v
```

## The database

One manual step the first time, because EDB does not publish a stable URL
for the binaries zip. The script will download it for you using a link
pinned in the source, but that link goes stale with each point release —
when it does, get the current **Windows x86-64 "zip archive of the
binaries"** from <https://www.enterprisedb.com/download-postgresql-binaries>
and pass `--pg-url`, or download it and pass `--pg-zip`.

```bash
python scripts/setup_postgres.py
```

That downloads and trims PostgreSQL, reads the major version back out of
the extracted binaries, downloads the **matching** TimescaleDB build, runs
`initdb`, writes the config, starts the cluster on port 5433, creates the
role and database, applies pending migrations, and grants the app role its
privileges. Re-running is safe at every step.

```bash
python scripts/setup_postgres.py --status
python scripts/setup_postgres.py --stop
uv run python scripts/seed_prototype.py          # config -> database
uv run python scripts/run_poller.py --sink postgres
```

Deliberately the same sequence the installer will run on a customer PC:
private copy of Postgres, non-default port, loopback only, scram, generated
password, Timescale placed as files rather than by running its `setup.exe`.
What it does *not* do is register Windows services, create a service
account, or set directory ACLs — and that remaining part is the real risk
left in the installer.

Vendored payloads are pinned by SHA-256 in `installer/vendor/vendor.lock.json`.

## Ports

| | |
|---|---|
| Simulator | 5020 |
| PostgreSQL | 5433 — deliberately not 5432 |
| Mosquitto | 1883, planned |
| API + dashboard | 1885 |

## Conventions that already matter

- **Wire address = datasheet number − 40001.** `40001` is read with start
  address `0`. Off-by-one here is the classic Modbus bug.
- **Pressure is signed.** A reversed cascade sends `65531` for −5 Pa.
  Unsigned, that reads as sixty-five thousand pascals — hiding a real
  excursion and raising a nonsense alarm instead.
- **Silence is not an exception.** A device that does not answer returns
  `None`, is reported offline *once*, and is retried next pass.
- **`reading` is append-only for the app role.** `UPDATE` and `DELETE` are
  revoked, verified by test. A correction is a new record with a reason,
  never an edit in place — which is what Part 11 requires and what makes
  retrofitting it later expensive.
- **Quality is a column, not an absence.**
- **Inserts are idempotent.** Replay after a reconnection delivers at least
  once.
- **`consumption()` refuses non-counters.** An instantaneous value never
  accumulates, so there is nothing to bill; it returns the counters with
  status `not_a_counter` rather than an arithmetic difference that means
  nothing.
- **`registers.NEVER_WRITE`** lists registers that must never be written:
  slave address, baud rate, save, auto-zero.

## What measuring and running taught us

The dead-device problem had **two** causes, and neither fix alone was
enough. Measured with three live devices plus one absent, 2 s timeout, 2 s
configured interval:

| | cycles in 20 s | readings | s/cycle |
|---|---|---|---|
| as first written | 3 | 9 | 8.26 |
| backoff only | 6 | 18 | 4.17 |
| `retries=1` only | 5 | 15 | 4.20 |
| **both** | **8** | **24** | **2.50** |

- **`offline_backoff_cycles`**, ported from Meril EMS. A device that fails
  is skipped entirely for five passes. Fixed rather than exponential: a
  repaired device must be noticed within a bounded time.
- **`retries=1` on the pymodbus client.** pymodbus defaults to **three**,
  so every timeout silently cost three times what the config said. The
  poller already owns retry policy, so retrying in the library too was
  double counting — and it blocked healthy devices while it happened.

Building the dashboard added four more, three of which only a rendered
page could reveal:

7. **Every excursion bar was invisible.** `.barfill` is a `<span>`, and an
   inline element ignores `width` — so bars computed at a perfectly
   correct 100% / 46.8% rendered at **zero pixels**. The markup was right,
   the CSS was wrong, and nothing but looking at the page would have said
   so.
8. **A path counted parents instead of searching.** `parents[4]` from
   `web/app.py` lands on `packages/`, not the repo root, so the server
   refused to start with "No database". It now walks up looking for a
   marker file, which cannot be miscounted.
9. **PostgreSQL kept dying when a shell exited.** Not a crash — the
   Windows **job object** the shell runs in kills everything inside it,
   and `DETACHED_PROCESS` does not escape a job. `CREATE_BREAKAWAY_FROM_JOB`
   does. This is precisely why the installer must register Postgres as a
   **Windows service**, owned by the service manager rather than by
   whatever started it.
10. **Device ages displayed as "-1 s".** Two clocks: the poller stamps the
    reading in Python, `now()` is the database's transaction start, and a
    fraction of a second of skew goes negative. Clamped — but across a WAN
    to an edge agent the same skew would not be cosmetic, which is what
    §14's NTP requirement is for.

Six more defects came out of actually running things:

1. **`GatewayWorker` subclassed `threading.Thread` with an attribute named
   `_stop`**, shadowing an internal method `join()` calls. All 33 tests
   passed over it because none called `start()`. Now composes a thread, and
   two lifecycle tests cover it.
2. **`offline_backoff_cycles` was never wired into `load_config`** — the
   config key did nothing. A setting read from nowhere is worse than no
   setting, because you believe you have tuned something.
3. **The migration runner re-applied every file on every run**, so the
   second `setup_postgres.py` failed on "relation already exists". It now
   skips what `schema_migrations` records — which is the entire point of
   having that table.
4. **The app role had no privileges**: migrations run as superuser, so
   every object was owned by it. Now granted explicitly, least-privilege,
   with `reading` append-only.
5. **No command had a timeout**, so one blocked child turned the whole
   script into a silent hang with no indication of which step died. Every
   external command now has one.
6. **`consumption()` answered for instantaneous parameters**, returning
   "used = 1.0 Pa" for a differential pressure. Migration 0003 makes it
   refuse. It would have reached a report eventually.

## The dashboard

Served from `packages/core/src/jetpace_core/web/`. No build step: htmx and
ECharts are **vendored into `static/vendor/`** and hash-pinned in
`vendor.lock.json`, so the page makes zero external requests and works on
a plant network with no route to the internet.

Colours come from a validated data-visualisation palette, checked with the
validator rather than by eye — blue / orange / aqua pass the adjacent-pair
CVD gates in both light and dark modes. Aqua measures 2.74:1 on the light
surface, below the 3:1 bar, so the relief rule applies and every chart
ships direct labels and a table view. Status colours are reserved and
always carry an icon and a word, never colour alone.

Pressure is drawn as **small multiples** — one chart per room on a shared
scale — not as one multi-line chart. Six rooms would have needed six
categorical hues, and cycling past the validated three is not allowed; a
single series per chart takes identity from the title instead, and the
same layout still reads at the forty rooms a real facility has.

## Next

1. Mosquitto with `persistence true`, and an `MqttSink` implementing the
   same Protocol — nothing above it changes
2. Move the device list from `config/prototype.toml` into the database,
   with the dry-run diff Meril EMS already does well
3. The supervisor and the signed updater, ported from Meril RTD
4. Register Postgres as a Windows service (see below)

## Notes

- `postgresql.auto.conf` is written directly by the setup script. That file
  is really PostgreSQL's own, managed by `ALTER SYSTEM`; the installer
  should write a separate file and `include` it instead. Works, but wrong.
- The `DP_PV` scaling is not stated in the iDP-100 datasheet, and
  `40002`/`40003` are marked *Reserved* rather than part of the value, so
  it is modelled as a single signed 16-bit register in whatever unit
  `40014` selects. Confirm against a real device before it reaches a report.
- The datasheets under `Modbus Mapping/` belong to another project and are
  used here only as a realistic reference.
