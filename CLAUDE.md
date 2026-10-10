# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Jetpace EMS: an energy management platform for 400+ electricity meters (Modbus TCP/RTU now, DLMS/BACnet/OPC UA later). It handles acquisition, a time-series historian, billing reports, trends, alarms, user roles and over-the-air updates for fleets of edge devices. It is sold as a product, so each customer gets their own install on their own site server.

It is the successor to **Meril EMS** (`D:\Meril EMS`), a working single-site system: 236 meters, 15 gateways, Fastify + SQL Server, polling every 120 s but writing only every 4 h. Meril stays in production while Jetpace is built. Reuse its code instead of rewriting it, especially `src/billing`, `src/modbus` and `src/config`.

## Status

No code yet, so there are no build, test or run commands. Add them here once `package.json` exists. Development starts at phase P0 (below).

The full design is in the architecture doc: https://claude.ai/code/artifact/fedca9ba-8360-4372-ae26-ea8cefab1db8. Read it with the Artifact tool (`action: "read"`) before any design work. The sections cited below (§N) refer to it.

## Architecture (decided)

```
meters -Modbus-> edge agent -MQTT Sparkplug B/TLS-> Mosquitto -> ingest -> PostgreSQL + TimescaleDB
                 (local SQLite buffer,                          -> rule engine -> notifier
                  replays on reconnect)                API (Fastify) + React/ECharts UI read the DB
```

- **Language:** TypeScript throughout. The edge agent may be rewritten in Go later; MQTT is the boundary between it and everything else, which is what makes that possible.
- **Storage:** PostgreSQL + TimescaleDB. Readings go in one narrow hypertable `reading(ts, device_id, param, value, quality)`, not one column per parameter. Config, users and audit live in ordinary tables in the same database.
- **Billing** is computed from continuous aggregates (`reading_hourly`, then `reading_daily` built on the hourly one) using `first()`/`last()`. Bucket days in the site's own timezone (`time_bucket(..., 'Asia/Kolkata')`) and never do date arithmetic in application code.
- **Auth:** Keycloak (OIDC, federated with Active Directory). **OTA:** self-hosted Mender. Updates go to edge agents and site servers only, never to meters.
- **Deployment:** Docker Compose on a Linux VM, one per site. A shared Jetpace control plane holds device identity, versions and health, and **never meter data**.

## Billing rules (must not change)

These come from Meril and are the core of the product. Any new billing code must reproduce them exactly.

- The **last** reading in a period closes it, never the highest.
- Counters never run backwards. A negative difference is reported as `counter_reset`, never billed as a negative amount.
- If the opening reading is too old and the meter already had history, the consumption is blank (`stale_open`) and both counter values are still shown.
- A meter with no earlier history is billed from its first reading (`partial_new_meter`).
- An idle meter keeps its last reading, so zero means zero. Missing data means `no_data`.
- A blank figure must always carry a status that explains it. Never swap a blank for a plausible guess.

Until cutover (P5), the Meril `vitest` suite and Meril's past reports are the reference: new reports must match them to the paisa, including blanks and statuses.

## Conventions

- Every table has a `tenant_id` and every query filters on it, even though each install serves a single tenant.
- `device.code` (e.g. `GW02-047`) never changes. Retire devices with `retired_at`; never delete a device that has readings.
- `device_model` rows are versioned by datasheet revision. When a register map changes, add a new row instead of editing the old one. Register maps stay pinned to the manufacturer datasheets by tests, as in Meril.
- Readings carry a `quality` flag (0 good, 1 stale/backfilled, 2 suspect). Reports must not silently mix backfilled data with good data.
- Ingest is idempotent (`ON CONFLICT (ts, device_id, param) DO NOTHING`) because store-and-forward delivers at least once.
- Modbus writes (Sparkplug `DCMD`) are disabled at build time.
- Schema migrations must keep working with the previous release: add columns, and don't drop one in the same release that stops using it.
- Zod schemas define the contracts between the edge agent, the API and the UI.
- The audit log is append-only. Nobody gets update or delete rights on it, administrators included.
- Every component must allow commercial redistribution. Grafana (AGPL) may be used only as an internal tool; use Valkey, not Redis.

## Delivery phases

Each phase ends with a gate that must pass before the next one starts:

- **P0:** add Timescale next to Meril's SQL Server and write every reading to both, with 1-minute logging on the new path.
- **P1:** move billing onto the continuous aggregates and compare against Meril's reports month by month.
- **P2:** move the poller into an edge agent behind MQTT, and build a simulator for 400 meters.
- **P3:** React UI.
- **P4:** Keycloak, alarms and Mender.
- **P5:** cut over from SQL Server.
- **P6:** ISO 50001 features, virtual meters, load-profile backfill and time-of-use tariffs.
