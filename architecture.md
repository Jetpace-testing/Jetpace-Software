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
    ├── core/                 jetpace-core        the platform and the UI shell
    ├── energy/               jetpace-energy      tariffs, billing, demand
    └── environment/          jetpace-environment ISO 14644, DP cascade
```

| Package | Depends on core as | Contains |
|---|---|---|
| `core` | — | devices, drivers, historian, alarms, identity, audit, reports, updater, UI shell |
| `energy` | workspace | tariffs, billing, demand, EnPI |
| `environment` | workspace | ISO 14644, DP cascade, Part 11 |

All three exist. Each product depends on core; neither depends on the other, and
core depends on neither — enforced by `test_core_names_no_product.py`, which
scans core's source rather than trusting the layout.

### The pinning asymmetry, and why it is not here

This section used to say that environment pins a validated core version (`==
1.4.2`) while energy tracks head, so **an energy release cannot reach a
qualified cleanroom installation**, and called that asymmetry "the whole design".

**One uv workspace cannot express it.** `{ workspace = true }` means "resolve to
the local member", and `uv.lock` records exactly one `jetpace-core`. If core
moves to 0.2.0 and environment asks for `==0.1.0`, `uv lock` fails. There is no
arrangement of this repository in which one lock file holds two core versions.

Writing `==` in `packages/environment/pyproject.toml` would buy *friction* — a
core version bump could not be committed without also editing that line — but it
would not isolate anything, and leaving the claim in this document unqualified
meant believing a qualified site was protected by something that does not run.
So the pin is not there, and the guarantee is not claimed.

Delivering it needs one of:

- **core published as a versioned artifact** to a private index, with environment
  depending on a real version rather than a workspace path; or
- **environment in its own repository**, pinning core the same way; or
- **a bundle per product**, built from two checkouts — core at the validated tag
  for environment, at head for energy.

The third is the only one that also separates the *schema*: `consumption()` is
energy's and lives in core's migrations permanently (debt #12). None is needed
until there is a validated customer install, and all three are cheaper now that
the packages exist than they would have been before. Recorded as debt #11.

---

## 3. Module boundaries

Inside `packages/core/src/jetpace_core/`:

```
decode.py          registers → numbers.      imports: struct
devices.py         Register, DeviceModel.    imports: nothing
modbus_client.py   talk to one gateway.      imports: pymodbus
poller.py          threads, scheduling.      imports: decode, modbus_client
mqtt.py            topics and payloads.      imports: json, datetime
sinks.py           where readings go.        imports: poller (Reading), mqtt
web/               the shell.                imports: psycopg, fastapi
web/products.py    what a product may add.   imports: fastapi
```

`registers.py` was here, holding both the generic `Register`/`DeviceModel`
scaffolding and the iDP-100 differential pressure map. The map is one product's
and moved to `jetpace_environment.registers`; the scaffolding is the platform's,
because "wire address = datasheet number − 40001" is true of any Modbus device
and is an invariant in CLAUDE.md, so it stayed as `devices.py`.

**Dependencies point one way: downward in that list.** The rules that matter:

- **`decode.py` and `devices.py` import nothing of ours.** They are pure. Every
  test over them runs with no socket, no database and no clock.
- **core imports no product.** Not `jetpace_energy`, not
  `jetpace_environment`, not in a function body. A product is reached only
  through `web/products.py`, which resolves a slug read from
  `config/products.toml` with `importlib`. Checked by scanning core's own source.
- **`poller.py` must not know what a sink is.** It is handed one and calls it.
- **`poller.py`, `decode.py` and `modbus_client.py` contain zero MQTT
  references.** Verified, and worth re-verifying: this is what made adding a
  third transport a change to `sinks.py` and two entry points, and nothing else.
- **`web/` is read-only.** It never writes a reading. Its database role has
  `UPDATE` and `DELETE` revoked on `reading`, and no grant at all on `device` —
  which is why the overview's Add Device button opens an explanation rather than
  a form. The CSV exports are reads like any page, bounded by a window clamp and
  a row cap, and as unauthenticated as everything beside them until §10 puts a
  login in front of the whole application.
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

### The product seam

The second interface, and the one that makes §2's package table real. Core owns
the shell — base template, overview, device pages, chart furniture — and must not
name a product. So a product describes itself and core composes it.

`jetpace_core.web.products.Product` is that description: a slug, a label, a
template directory, an optional static directory and stylesheet, and a
`routes(shell)` callable. Each product package exposes exactly one public name,
`jetpace_<slug>.web.product()`, returning one. `create_app(dsn, products=())`
then:

- builds **one** Jinja environment over `[core/templates, *product templates]`,
  so a product page can `{% extends "_base.html" %}` and get the real shell;
- mounts each product's static at `/static/<slug>` **before** core's `/static`,
  because a mount at `/static` matches everything beneath it and would otherwise
  swallow every product asset;
- renders the navigation from the loaded products, so a customer who bought one
  product has no tab for the other;
- includes each product's router last, so a product cannot shadow `/healthz`.

**Three things a product contributes beyond its own pages**, all optional, all
with the same rule: the product formats the value, because the product owns the
units and the vocabulary.

- `tiles(conn) -> list[Tile]` — figures on the shared overview. Pascals to one
  decimal place is the environmental product's knowledge; handing core a float
  and a format string would put a product's units back into the shared layer by
  the back door. `state` is `""`, `"good"` or `"alarm"` and `tone` is one of six
  named colours, both validated in `__post_init__`, so a product cannot invent a
  status vocabulary beside the shell's.
- `events(conn) -> list[Event]` — exceptions on the shared overview. The shell
  knows a device has gone silent and that the poller flagged a sample, because
  both are facts about data arriving. It cannot know that 0 Pa across a cleanroom
  door is a failure, because that needs the specification, and the specification
  is the product's. `kind` is `"silent"`, `"suspect"` or `"excursion"` and
  nothing else. **This is not the alarm model** — see §10; nothing is stored and
  nothing can be acknowledged, because there is no alarm row and no user to
  attribute an acknowledgement to.
- `stylesheet` — linked only when the product is loaded, so a cleanroom install
  does not fetch the energy CSS.

Both callables are invoked **inside a per-product `try`**. A product whose query
fails must not blank the first screen, and must not vanish quietly either: it
gets a tile and a row saying so, in its own place, and the rest of the page is
unaffected. A site running for a week with a figure silently missing is the
failure being prevented.

**Resolution is by name, not by entry point.** `importlib.import_module` on a
slug from `config/products.toml`. Entry points would be the idiomatic answer and
they do not survive freezing: `build.py` ships no distribution metadata, so a
frozen bundle would discover zero products and come up healthy with no product
pages and nothing in any log. The price of importing by name is that PyInstaller
cannot see it either — paid in `build.py`'s `product_hidden_imports()`, and
checked by `smoke_test()`, which runs `jetpace where` against the frozen
executable and fails the build if a product is missing.

Two deliberate non-choices worth recording. The product selection is **not** in
`version.json`, because that file is in `REPLACEABLE` and every OTA update would
reset it. And it is **not** read through `cli.load_config`, which returns the
gateway list and exits when there are no gateways — the dashboard should not
fail because the device list is empty.

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
| Device simulator | 5020 | supervisor child | **yes** (dev only) |
| Poller | — | supervisor child | **yes** |
| Ingest | — | supervisor child | **yes** |
| Dashboard, administrative | 18850 (dev) / 1885 | supervisor child | **yes** |
| Dashboard, per product | base + 1, + 2 … | supervisor child | **yes** |

**No port is a default.** 1883, 1884 and 1885 were all occupied on one
development laptop — Titan EMS owns 1883 — and a customer may already run
PostgreSQL on 5432. Ports are settings, not constants.

### One dashboard per product

`jetpace web` serves one set of products, chosen with `--products` or
`--shell-only`, so a site runs several instances from the one installation:

| Instance | Products | Binds | For |
|---|---|---|---|
| `web` | all enabled | `127.0.0.1` | whoever looks after the system |
| `web-<slug>` | that one | `--product-host`, default `127.0.0.1` | the people who use that product |

Every instance carries the shell, because the device list is not an
administrative screen — a customer who bought one product still needs to see
their own devices. The administrative instance is therefore not a cut-down one;
it is the *wider* one, and what keeps a customer out of it is the interface it
binds to.

**A separate port is a separate URL, not a separate permission.** Whoever can
reach the energy dashboard can type the administrative port, and nothing in this
software authenticates anybody — §10 is unbuilt. Of this arrangement only
`--host` is actually enforced, and by the operating system rather than by us: a
dashboard on `127.0.0.1` cannot be reached from another machine at all. That is
a real boundary and it is the only one here.

So for a customer who has bought one product, the stronger answer is not a port
but `config/products.toml` naming that product: the other product's routes then
do not exist, and there is nothing to find by guessing a URL. Separate ports
earn their keep at a site that runs **both** products and wants the facilities
team and the energy manager on separate URLs.

Ports for the product instances are derived from the base port unless pinned
with `--product-port <slug>=<port>`. Derivation is order-dependent and can land
on an occupied port — on the development laptop a base of 1885 derives 1886 and
1887, which are Meril RTD and our own broker — so `register_supervisor_service.py`
tries to bind each one and says so when it cannot.

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

### The health gate

`/healthz` is the supervisor's release gate and the installer's
finished-yet check. It is consulted **only while a swapped-in release is on
trial** — `_supervise()` never calls it — so a strict answer cannot put the
service into a restart loop during ordinary running. A failed gate makes the
supervisor exit; `sc failure` restarts it, the next start counts an attempt, and
three attempts roll the release back.

It answers two questions, and both must hold:

1. **Has a reading arrived since this process started?** Compared as durations —
   the database's measured age against our own uptime — never as two timestamps,
   so a clock or timezone disagreement cancels out instead of deciding the
   answer.
2. **Is the newest reading younger than `READING_AGE_LIMIT`** (120 s, raise with
   `JETPACE_HEALTH_AGE_LIMIT` for a slow scan class)? Above `OFFLINE_AFTER`'s
   90 s, because this asks whether *everything* has gone quiet rather than one
   device.

A system that has **never** logged anything is well. Nothing can regress from
nothing, and two callers depend on it: the installer waits for `ok:true` against
a database that is empty by definition, and a site that is installed but not yet
commissioned must still be able to take an update.

Both questions exist because of real faults. The endpoint originally asked
whether any reading existed in the last 24 hours, which stayed true for a day
after logging stopped — on 13 October it reported `ok:true` through half an hour
of silence. And freshness *alone* would not have fixed the gate: it runs seconds
after a swap, when the readings the **previous** version wrote are still fresh, so
a release that never logs anything would have passed on its first poll on the
strength of data it did not write. Question 1 is what a release has to earn;
question 2 is what catches logging stopping three hours later.

The reason travels. An unwell dashboard returns `why` in its body, and the
supervisor reads it off the 503 and logs it, because "HTTP 503" at a customer
site does not distinguish a broken release from a dead gateway.

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
| 1 | ~~Product logic in core~~ — **fixed, and now enforced.** `packages/energy` and `packages/environment` are workspace members. `dp`, `kwh_import`, both product templates, the cleanroom CSS and the iDP-100 map have all left core | `test_core_names_no_product.py` scans core's source for the parameter keys, for an import of either product, for a product selector in `app.css` and for a product link in `_base.html`. The two exceptions it deliberately does not cover are debt #12 and #3, both recorded |
| 2 | **No declared `Sink` Protocol.** `sinks.py` says "two implementations" and there are three; the shape is duck-typed and unenforced | The seam is the most load-bearing boundary in the system and nothing checks it. A `typing.Protocol` plus a conformance test would |
| 3 | Device list in `config/prototype.toml`, not the database | §6. Needs a dry-run diff when it moves |
| 4 | Mosquitto is not a Windows service | Dies on reboot; the same treatment as PostgreSQL would fix it |
| 5 | OTA cannot replace the supervisor itself | A running executable holds its own file open, so `REPLACEABLE` is `app/` + `version.json` only. Supervisor changes go out through the installer — which is why it must stay small and stay still |
| 6 | `DP_PV` scaling unconfirmed against a real device | A scale error reaches a batch record |
| 7 | Site installer runbook §2 claims Mosquitto binaries are "extracted from the Windows build" | Impossible — all published builds are installers. The runbook is wrong |
| 8 | ~~`energy` and `environment` packages do not exist~~ — **fixed.** Both are workspace members with their own queries, routes, templates and tests | What remains is not the packaging but the version isolation: debt #11 |
| 9 | **`zipfile.extractall` must never be used on a release** | It sanitises silently — strips leading slashes, drops `..` — and carries on. `archive.py` refuses instead, because such a name means the release was built wrong or built by someone else. Enforced by 53 tests |
| 10 | The release key in `key.py` is a **development** key | Generated on the build laptop, private half in gitignored `.local/release-key.pem`. Must be replaced before the first customer install, and custody written down |
| 11 | **No installer, and no vendored payload inside it.** `installer/` holds only `vendor/`; there is no NSIS package, and `setup_postgres.py` still downloads 350 MB at run time | A plant PC has no Python and often no internet. The 169 MB in `installer/vendor/` has to ship inside the package. Mosquitto also still needs its own installer run by hand first, because it publishes no zip |
| 12 | **Environment does not pin a validated core version**, so an energy change does reach a cleanroom install | §2 has the three routes to fixing it and what each costs. Not needed until there is a qualified customer install; much cheaper now the packages exist. The claim has been removed from §2 rather than left standing unqualified |
| 13 | **`consumption()` is an energy function in core's schema, permanently.** `migrations/0002` defines it and `0003`/`0004` rewrite it | Migrations are numbered, forward-only and already applied at every install, so they cannot move or be renumbered. Core's *Python* never calls it, which is what the boundary test checks. Per-product migration chains are the real fix and are only mandatory once products ship separately — see debt #12 |
| 14 | **One frozen bundle carries both products**; which one a site serves is `config/products.toml` | Deliberate: it keeps `REPLACEABLE`, the NSIS file lists and the one-version-per-release-zip assumption untouched, which matters because the updater must work before anything ships. The cost is that an environment-only customer has the energy code on disk, unreachable but present — which a validated install may eventually object to |
| 15 | ~~`/healthz` asserts a 24-hour reading count, not freshness~~ — **fixed.** It now requires a reading to have arrived since the process started **and** to be younger than `READING_AGE_LIMIT` | Found the hard way on 13 October: readings stopped at 16:55 and the gate stayed green with 67k still inside the window. Fixing it surfaced a second bug the first had been hiding — freshness alone would still have passed, because the *previous* version's readings are seconds old when the gate runs. See §5 |
| 16 | **Several dashboards, one health gate.** The supervisor watches one `health_url`; a single product instance failing is caught only by child restart | Each instance answers for itself on its own port, but `Supervisor.from_config` takes one URL. Widening it means editing `supervisor.py`, which debt #5 says can only reach a site through the installer — so this waits for a reason better than tidiness |
| 17 | **The exceptions panel's suspect query has no index behind it.** `reading.quality` is unindexed, so `SUSPECT` is a scan of whatever window it is given — bounded to 6 hours and polled every 30 s, which is affordable on this prototype and will not be on a 400-meter site | The fix is one line, `CREATE INDEX ... ON reading (ts DESC) WHERE quality <> 0`, and it is cheap because almost no row qualifies. It is a migration against a running install, which is not something to slip into a screen build — so it is recorded here and goes out with the next schema change |
| 18 | **No report generator.** The overview's panel offers CSV exports produced on request, not the scheduled, signed PDFs the design draws | A GxP site needs a report with a name on it, which needs the user model (§10). The exports are real and bounded; what is missing is attribution and scheduling, not data |
| 19 | **Three polled regions on the overview, one connection each.** `/partials/live` (5 s, four panels via out-of-band swaps), `/partials/device-card` (15 s) and `/partials/exceptions` (30 s) | Each request opens its own connection — `queries.connect` has no pool. Fine at one dashboard; three product instances on one machine is nine connections of churn, and a pooler is the answer before a site with several operators |

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
3. ~~**Split the product logic out of core**~~ — debt #1 and #8 done; `energy`
   and `environment` are packages and core cannot reach either. Debt #2, the
   declared `Sink` Protocol, was listed with it and is still open.
4. **Device list into the database** with a dry-run diff.
5. **Mosquitto as a service.**
6. **The alarm model.** Not cosmetic, and now the single largest gap the
   frontend keeps running into. The Figma design's first screen has an Active
   Alarms figure, a severity column, a Recent Alarms panel, an Acknowledge
   action and an alarm filter — five features, all of which need the same
   missing thing: a limits table, an alarm row with a lifecycle, and a user to
   attribute an acknowledgement to. What is built instead is `Product.events`
   (§3), which derives exceptions on each request from silence, flagged samples
   and each product's own specification, and says in the panel that it is doing
   so. *Gate: an alarm raised by a limit survives a restart, and an
   acknowledgement is attributable.* Meril RTD already has all of this; reuse it
   rather than rewriting.

**Deliberately not built, and what each would need.** Recorded here so the next
person does not take the design as a to-do list:

| The design draws | Why it is not here |
|---|---|
| A floor plan with named zones at fixed coordinates | There is no room, zone or coordinate in the schema — a device belongs to a gateway. The panel groups by gateway, which is a real area, and lays the zones out rather than positioning them |
| Temperature, humidity and pressure columns on every device row | Its sample data has all three on every device; real instruments do not. The reading column follows whatever parameter each device actually has |
| An Add Device form | The application has no write path at all; the app role's grant on `reading` is append-only and it has none on `device`. The modal explains commissioning instead. A real form needs the user model, because an unattributed change to a register map is not something a validated site can accept |
| Three finished PDF reports with file sizes | Debt #18 |
| Upper and lower limit lines on the trend | Same missing limits table as the alarm model. A threshold somebody could read a specification off would be the most dangerous thing on the page |
| "Range 18–24 °C" on each parameter card | Likewise. The cards show the observed 24-hour range instead, labelled as observed |
| 7–10px type throughout | Readable on a 27-inch mockup, not on a plant PC at arm's length. `app.css` carries a `--t-N` ramp where N is the design's size and the value is ours, one step larger; checking a declaration against the Figma file still works, and the scale moves from one block |
| `repeat(7, …)` for the figures row | The design has exactly seven figures. We have four plus whatever the loaded products contribute — four, seven or nine — so the column count is computed per install and the rows are balanced rather than filled |

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
