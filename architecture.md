# Jetpace Software — engineering architecture

**Status:** living document · **Updated:** 12 October 2026 · **Language:** Python 3.12

## What this document is

The rules this repository is built by: what the packages are, what may import
what, what must never change, and what is actually built today versus merely
designed.

It is **not** the platform specification. That is a separate, longer document
covering the business case, deployment, licensing, GxP obligations, PC
specification and the delivery plan:

> **Platform Architecture, Revision E** — <https://claude.ai/artifact/YULvyznhQxLwfUCGuajncP>

Sections cited below as **§N** refer to Revision E. Where the two disagree,
Revision E is the specification and this file is the record of reality — and the
gap between them is listed under [Drift and debt](#drift-and-debt) rather than
hidden.

Read this file before changing code. Read Revision E before changing a design.

---

## 1. Where the repo actually stands

One prototype, running end to end on a single Windows PC. A reading travels
Modbus → poller → MQTT → ingest → TimescaleDB → dashboard, verified live.

| Piece | State | Notes |
|---|---|---|
| Register decode | **built** | `decode.py`, 2's complement and IEEE-754 |
| Modbus TCP client | **built** | pymodbus wrapper, `retries=1` |
| Poller | **built** | one thread per gateway, offline backoff |
| Device simulator | **built** | hand-written Modbus TCP server, 3+ units |
| Historian schema | **built** | 4 migrations, hypertable, 2 continuous aggregates |
| MQTT topics + payloads | **built** | pure functions, no broker needed to test |
| Ingest consumer | **built** | batches, idempotent, rejects malformed |
| Dashboard | **built** | FastAPI + Jinja + HTMX + ECharts, assets local |
| PostgreSQL as a service | **built** | `jetpace-postgres`, auto-start, NETWORK SERVICE |
| Frozen build | **built** | `scripts/build.py` — app 43 MB, supervisor 33 MB |
| Signed manifest + verify | **built** | Ed25519, fail-closed on an unset key |
| Swap, trial, auto-rollback | **built** | `pending.json` state machine, two-pass swap |
| Supervisor + health gate | **built** | children, backoff, `/healthz` trial |
| Release signing tool | **built** | `scripts/make_release.py` |
| Download sources | **built** | GitHub Releases, and a folder for a USB stick |
| Safe archive reading | **built** | refuses escapes, bombs, symlinks, strays |
| Install / rollback CLI | **built** | `scripts/update.py` |
| Supervisor as a Windows service | **built** | `jetpace-supervisor`, auto-start, `sc failure` |
| **Update button (API + UI)** | **not built** | §12 — the CLI exercises the same path |
| Rule engine / alarms | not built | §9 |
| Identity, roles, audit | not built | §10 |
| Reports (Excel, PDF) | not built | §13 |
| Cloud rollup push | not built | §8 |
| `jetpace-energy` | not built | §3 |
| `jetpace-environment` | not built | §3 |

354 tests, all passing. 243,648 seeded readings. Schema version 4.

**Nothing can ship to a customer site until the updater works** (§12). That is
not a preference — a deployed install with no way to receive a fix is a liability.
That now works end to end: a release is signed, fetched, verified, unpacked
under protest, swapped in, put on trial behind a health gate, and rolled back
automatically after three failed starts. What is missing is only the button —
`scripts/update.py` drives exactly the path the web interface will call.

### The install layout

```
<install root>/
  app/                 the frozen application; jetpace.exe + _internal/   REPLACEABLE
  version.json         what app/ currently is                             REPLACEABLE
  supervisor/          the frozen supervisor, its own bundle
  config/              prototype.toml
  migrations/          numbered SQL, run at install time
  data/                secrets, the database, logs           (.local/ in a checkout)
  update/              staging, backup, failed, downloads
```

Only the first two are in `REPLACEABLE`, and the rest of the list is the
reason. The supervisor cannot be in it — a running executable holds its own
file open. `data/` must not be in it, or the first successful update would
delete a customer's readings, passwords and logs silently. One executable
serves all three jobs (`jetpace.exe poller|ingest|web`) because three bundles
would be three copies of Python, psycopg, pymodbus and uvicorn.

**Proven end to end on real frozen binaries**: a 43 MB `app/` at 1.0.0,
a signed 19.7 MB release of 1.0.1, downloaded, hash-checked, name-checked,
unpacked, swapped in, and the frozen executable afterwards reporting 1.0.1
with 1.0.0 kept as the backup.

### How an update runs

```
   make_release.py  ──sign──▶  manifest.json + jetpace-<ver>.zip
                                        │
               check signature → download → check SHA-256 →
               check every name → check the version inside → unpack
                                        ▼
                               update/staging/  +  pending.json {staged: true}
                                        │
   supervisor starts ──▶ prepare() ──swap──▶ app/   old app → update/backup/<ver>/
                                        │           pending.json {staged: false, attempts: 1}
                                        ▼
                        start children, poll /healthz
                           │                     │
                      healthy                 not healthy
                           │                     │
                      confirm()            exit non-zero
                   pending.json gone      (service manager restarts;
                     update over           prepare() counts the attempt;
                                            after 3, the backup goes back)
```

The supervisor never performs the rollback itself. It exits, and the next start
counts the attempt. That path still works when the supervisor is the broken
thing, which a rollback it had to carry out successfully would not.

---

## 2. Packages and the workspace

A `uv` workspace. One lock file, one interpreter version, members under
`packages/`.

```
Jetpace Software/
├── pyproject.toml            [tool.uv.workspace] members = ["packages/*"]
├── uv.lock                   one lock for the whole workspace
├── architecture.md           this file
├── migrations/               numbered SQL, forward-only
├── scripts/                  entry points and setup; not importable library code
├── tools/sim/                device simulators
└── packages/
    └── core/                 jetpace-core        ← the only member today
```

The target shape, per §3:

| Package | Depends on core as | Contains |
|---|---|---|
| `core` | — | devices, drivers, historian, alarms, identity, audit, reports, updater, UI shell |
| `energy` | workspace, **tracks head** | tariffs, billing, demand, EnPI |
| `environment` | **pinned**, e.g. `== 1.4.2` | ISO 14644, DP cascade, Part 11 |

That asymmetry is the whole design. Energy gets core improvements immediately.
Environment pins a core version it has validated, so **an energy release cannot
reach a qualified cleanroom installation**. Retrofitting that separation after a
site is validated is far worse than carrying it from the start.

---

## 3. Module boundaries

Inside `packages/core/src/jetpace_core/`:

```
decode.py          registers → numbers.      imports: struct
registers.py       the iDP-100 map.          imports: nothing
modbus_client.py   talk to one gateway.      imports: pymodbus
poller.py          threads, scheduling.      imports: decode, modbus_client
mqtt.py            topics and payloads.      imports: json, datetime
sinks.py           where readings go.        imports: poller (Reading), mqtt
web/               the dashboard.            imports: psycopg, fastapi
```

**Dependencies point one way: downward in that list.** The rules that matter:

- **`decode.py` and `registers.py` import nothing of ours.** They are pure. Every
  test over them runs with no socket, no database and no clock.
- **`poller.py` must not know what a sink is.** It is handed one and calls it.
- **`poller.py`, `decode.py` and `modbus_client.py` contain zero MQTT
  references.** Verified, and worth re-verifying: this is what made adding a
  third transport a change to `sinks.py` and two entry points, and nothing else.
- **`web/` is read-only.** It never writes a reading. Its database role has
  `UPDATE` and `DELETE` revoked on `reading`.
- **`scripts/` is not a library.** Nothing in `packages/` may import from it.
  Scripts compose; they do not define.

If a change needs one of these reversed, the boundary was wrong — say so and move
it deliberately. Do not add an import that quietly contradicts this list.

### The sink seam

The one interface that has already earned its keep. Three implementations,
structurally identical:

```python
def write(self, readings: list[Reading]) -> None: ...
def device_state(self, device_code: str, online: bool, detail: str = "") -> None: ...
def close(self) -> None: ...
```

`ConsoleSink` prints. `PostgresSink` writes rows. `MqttSink` publishes. The poller
is constructed with one and cannot tell which it has.

This is why a direct-write mode still exists for an install too small to want a
broker, and why the poller could be reimplemented in a faster language later
without anything upstream noticing. **Ingest reuses `PostgresSink` rather than
re-implementing writes**, so exactly one piece of code knows how a reading
becomes a row.

---

## 4. Data model

Configuration, identity and audit are ordinary tables. Readings are one narrow
hypertable. Full DDL in §6; what matters when writing code:

```sql
reading (ts, device_id, param, value, quality)
```

- **Narrow, not a column per parameter.** Different devices expose different
  parameters and the two products expose entirely different sets. A wide table
  forces a migration whenever a customer adds a parameter. Segmenting
  compression by `(device_id, param)` stores each series as its own run, which is
  the access pattern of both trends and billing.
- **`quality` is a column, not an absence.** 0 good, 1 substituted, 2 suspect.
  Both existing systems distinguish "read zero" from "did not answer" by whether
  a row exists; that breaks the moment backfill inserts rows of mixed
  provenance. A report must be able to say it includes substituted samples
  instead of quietly averaging them in. Under §11 that is a regulatory
  requirement.
- **`device.code` never changes** (e.g. `GW02-047`). Retire with `retired_at`;
  never delete a device that has readings.
- **`device_model` rows are versioned by datasheet revision.** A moved register
  is a new row, never an edit, so history stays decodable. Maps are pinned to
  datasheets by tests.
- **Every table has `tenant_id` and every query filters on it**, even though each
  install serves one tenant. Adding a column you do not need is free; retrofitting
  tenancy is a refactor always attempted under commercial pressure.
- **Ingest is idempotent** — `ON CONFLICT (ts, device_id, param) DO NOTHING` —
  because QoS 1 delivers at least once.

### Migrations

Forward-only, numbered, each applied once and recorded in `schema_migrations`.

| | |
|---|---|
| `0001_init.sql` | tenancy, the hypertable, quality |
| `0002_rollups.sql` | hourly + daily continuous aggregates, `consumption()` |
| `0003_consumption_guard.sql` | refuses to answer for non-counters |
| `0004_consumption_period_start.sql` | snaps periods to day boundaries |

A migration must keep working with the previous release: add columns, and do not
drop one in the same release that stops using it.

---

## 5. Processes, ports and lifecycle

| Process | Port | Owner | Survives reboot |
|---|---|---|---|
| PostgreSQL + TimescaleDB | 5433 | Windows service `jetpace-postgres` | **yes** |
| Mosquitto | 1887 | **Windows service** `jetpace-mosquitto` | **yes** |
| Device simulator | 5020 | foreground | no (dev only) |
| Poller | — | foreground | no |
| Ingest | — | foreground | no |
| Dashboard | 1885 | foreground | no |

**No port is a default.** 1883, 1884 and 1885 were all occupied on one
development laptop — Titan EMS owns 1883 — and a customer may already run
PostgreSQL on 5432. Ports are settings, not constants.

All three long-lived pieces are services. `jetpace-supervisor` depends on
`jetpace-postgres`, so Windows starts the database first, and `sc failure`
restarts the supervisor when it exits — which is what drives the update
rollback, not a nicety.

**Mosquitto was the one that proved the point.** It was a detached process, so
the 13 October reboot left the broker down while the supervisor dutifully
restarted a poller and an ingest consumer that had nothing to connect to. Sixteen
hours of `ConnectionRefusedError`, a dashboard serving the previous day's
numbers, and no single thing broken. A dependency that is not a service is a
dependency that is not there after a reboot.

The installer's own `mosquitto` service must stay **disabled**: it uses the
default config, so it binds 1883, which Titan EMS owns. Both can bind it without
erroring — Titan takes the wildcard address, Mosquitto takes loopback, and
Windows permits the overlap — but loopback is the more specific match, so
Mosquitto silently wins every local connection intended for Titan.

### Rules every process must obey

- **Tolerate a dependency being absent at startup, and returning later.** Learned
  the hard way: the dashboard exited with code 4 after a weekend reboot because
  PostgreSQL was not running. A read-only dashboard that dies because the
  database blinked is its own fault. `web/` now opens a connection per request,
  which is why it survived a later service restart untouched.
- **An open port is not a ready service.** PostgreSQL binds its socket before it
  accepts connections and answers `FATAL: the database system is starting up`.
  Readiness is proven by a successful query, never by a TCP connect.
- **Every client identifier must be unique per process.** A hardcoded MQTT client
  ID made two pollers evict each other 28 times in 25 seconds. Use
  `f"...-{os.getpid()}"` or equivalent.
- **A child must not outlive the supervisor.** Windows does not kill children with
  their parent, so a hard-killed supervisor left all four running with their ports
  bound — and the orphaned web process kept answering `/healthz` with `ok:true`,
  which would confirm a broken release as good. Children are put in a job object
  with `KILL_ON_JOB_CLOSE`. The exact mirror of `CREATE_BREAKAWAY_FROM_JOB` in
  `setup_postgres.py`, where the database must survive its shell.
- **Broker persistence is not optional.** Without `persistence true` Mosquitto
  holds retained and queued messages in memory only, so every retained device
  status vanishes on restart — and the updater restarts services on every
  release. It looks exactly like data loss from an unrelated cause.

---

## 6. Configuration

Today the device list lives in `config/prototype.toml`. **It belongs in the
database**, and moving it is on the list — with a dry-run diff, because silently
reconciling a device list against live hardware is how a commissioned device gets
orphaned.

The rule meanwhile:

| In a file | In the database |
|---|---|
| Where things are: ports, hosts, paths, DSNs | What things are: devices, models, parameters, limits, tariffs |
| What differs per machine | What differs per site, and is edited by a user |
| Read at startup | Read at runtime, audited when changed |

Secrets live in `.local/`, which is gitignored: `pg_password`, `app_password`,
`mqtt_password`. They are generated, never committed, and never printed into a
log or a chat.

PostgreSQL settings live in `pgdata/conf.d/jetpace.conf`, included via
`include_dir`. **Never write `postgresql.auto.conf`** — that file belongs to
PostgreSQL, `ALTER SYSTEM` writes it, and it is read last so it wins. Sharing it
means one `ALTER SYSTEM` silently drops our port and preload library, and our
next setup run silently discards whatever a DBA set. Neither logs anything.

---

## 7. Invariants

These are not refactorable. Changing one is a product decision, not an
implementation choice.

### Billing semantics

Carried from Meril EMS, where they are the core of the product. New billing code
must reproduce them exactly.

- The **last** reading in a period closes it — never the highest.
- **Counters never run backwards.** A negative difference is `counter_reset`,
  never a negative amount.
- A **stale opening reading** on a meter that already had history yields a blank
  (`stale_open`), with both counter values still shown.
- A meter with no earlier history is billed from its first reading
  (`partial_new_meter`).
- An idle meter keeps its last reading, so **zero means zero**. Missing data is
  `no_data`.
- **A blank figure always carries a status explaining it.** Never swap a blank for
  a plausible guess.

Until cutover, Meril's `vitest` suite and its past reports are the specification:
new reports must match to the paisa, including blanks and statuses.

### Safety

- **`reading` is append-only** for the application role. `UPDATE` and `DELETE`
  are revoked — a §11 requirement that cannot be added later by policy.
- **The audit log is append-only.** Nobody gets update or delete rights,
  administrators included.
- **Modbus writes are disabled at build time**, not by configuration.
- **`registers.NEVER_WRITE = {40021, 40022, 40031, 40032, 40034}`** — slave
  address, baud rate, save, auto-zero. Writing any of these to a commissioned
  device loses contact with it, and recovery is a site visit with a laptop and a
  screwdriver.

### Decoding

- **Signed values are two's complement.** A reversed pressure cascade sends 65531
  for −5 Pa. Reading that as unsigned is not a rounding error, it is a wrong sign
  on a regulated measurement.
- **Floats are IEEE-754 big-endian across two 16-bit registers.**
- **Datasheet numbers are not wire addresses**: wire = datasheet − 40001.
- Register maps stay pinned to the manufacturer datasheets **by tests**.

### Licensing

Every component must allow commercial redistribution. Grafana (AGPL) may be used
only as an internal tool. Use Valkey, not Redis.

---

## 8. Running and testing

```bash
uv run pytest -q                                  # 70 tests, no network, no database
python scripts/setup_postgres.py --start          # or the service, if registered
python scripts/setup_mosquitto.py --start
python tools/sim/idp100_sim.py --units 3
uv run python scripts/run_poller.py --sink mqtt
uv run python scripts/run_ingest.py
uv run python scripts/run_web.py                  # http://127.0.0.1:1885
```

The test suite needs nothing running. That is deliberate and worth protecting:
topics, payloads and decoding are pure functions precisely so the shape of what
goes on the wire is pinned down where it is cheap, rather than discovered at a
site.

Tests passing is necessary and not sufficient. Running the prototype live found
three faults no test had: the duplicate client ID, queued-while-disconnected
being counted as failed, and that an MQTT will is not instant — it lands within
1.5× keepalive. **Run it before believing it.**

---

## 9. Drift and debt

Honest register of where the repo and the specification disagree.

| # | Issue | Why it matters |
|---|---|---|
| 1 | ~~Product logic in core~~ — **fixed.** The shell (overview, device list, device page, trend API) names no parameter key; `dp` and `kwh_import` appear only under `/environment` and `/energy` and in the queries those call | Enforced by tests asserting the shell's SQL contains no literal key. The remaining step is §3 proper: `packages/energy` and `packages/environment` as workspace members, with environment pinning a validated core version |
| 2 | **No declared `Sink` Protocol.** `sinks.py` says "two implementations" and there are three; the shape is duck-typed and unenforced | The seam is the most load-bearing boundary in the system and nothing checks it. A `typing.Protocol` plus a conformance test would |
| 3 | Device list in `config/prototype.toml`, not the database | §6. Needs a dry-run diff when it moves |
| 4 | Mosquitto is not a Windows service | Dies on reboot; the same treatment as PostgreSQL would fix it |
| 5 | OTA cannot replace the supervisor itself | A running executable holds its own file open, so `REPLACEABLE` is `app/` + `version.json` only. Supervisor changes go out through the installer — which is why it must stay small and stay still |
| 6 | `DP_PV` scaling unconfirmed against a real device | A scale error reaches a batch record |
| 7 | Site installer runbook §2 claims Mosquitto binaries are "extracted from the Windows build" | Impossible — all published builds are installers. The runbook is wrong |
| 8 | `energy` and `environment` packages do not exist | Expected at this stage; listed so the layout is not mistaken for the target |
| 9 | **`zipfile.extractall` must never be used on a release** | It sanitises silently — strips leading slashes, drops `..` — and carries on. `archive.py` refuses instead, because such a name means the release was built wrong or built by someone else. Enforced by 53 tests |
| 10 | The release key in `key.py` is a **development** key | Generated on the build laptop, private half in gitignored `.local/release-key.pem`. Must be replaced before the first customer install, and custody written down |
| 11 | **No installer, and no vendored payload inside it.** `installer/` holds only `vendor/`; there is no NSIS package, and `setup_postgres.py` still downloads 350 MB at run time | A plant PC has no Python and often no internet. The 169 MB in `installer/vendor/` has to ship inside the package. Mosquitto also still needs its own installer run by hand first, because it publishes no zip |

---

## 10. What comes next

Phases are per §16, each with a gate that must pass before the next starts.
Immediate order:

1. **Finish the updater** — the fetch half, debt #9. A safe extractor first,
   because it is the security-relevant piece, then a GitHub source and a folder
   source for a site with no internet. *Gate: a staged update applies and rolls
   back on a clean Windows VM.*
2. **Supervisor as a Windows service**, debt #5, with `sc failure` restart. Until
   then the rollback counter has nothing driving it.
3. **Split the product logic out of core** — debt #1 and #2 together, while the
   surface is still small.
4. **Device list into the database** with a dry-run diff.
5. **Mosquitto as a service.**

Then P1 onward from §16: billing onto the aggregates, validated against twelve
months of Meril reports to the paisa.

### Open decisions

Business calls, not technical ones. Defaults from §17 apply until answered.

| Question | Default if unanswered |
|---|---|
| Who holds the release private key, and where is the backup? | Offline, two copies, documented custody |
| Which customer is the canary? | An internal Jetpace install first |
| Is the environmental product for sale, or for Meril's own plants? | For sale; build the validation package |
| Does the energy product also need Part 11? | No, but it inherits the audit trail anyway |

Losing the release key means **no site can ever be updated again**. That one is
worth settling before the updater is written, not after.
