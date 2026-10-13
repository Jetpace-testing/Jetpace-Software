-- 0001_init.sql
--
-- Configuration, identity and readings. Applied by the migration runner in
-- order; recorded in schema_migrations so a build can refuse to start
-- against a database that is ahead of what it understands.
--
-- Two decisions from the architecture specification are load-bearing here
-- and should not be "tidied" later:
--
--   The readings table is NARROW - one row per (time, device, parameter) -
--   not one column per parameter. Different devices expose different
--   parameters, and the two products expose entirely different sets, so a
--   wide table would need a migration every time a customer adds a
--   parameter and would fill with nulls. Compression segmented by
--   (device_id, param) stores each series as its own run, which is the
--   access pattern of both trends and billing.
--
--   Quality is a COLUMN, not an absence. Distinguishing "read zero" from
--   "did not answer" by whether a row exists works until backfill starts
--   inserting rows of mixed provenance. An explicit flag lets a report say
--   a figure includes substituted samples instead of quietly averaging
--   them in - which under 21 CFR Part 11 is a requirement, not a nicety.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer     PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    name        text        NOT NULL
);

-- ---------------------------------------------------------------------------
-- tenancy
--
-- Every install serves one customer, but tenant_id is on every table and
-- every query filters by it from the first migration. Adding a column you
-- do not yet need is free; retrofitting tenancy is the refactor that is
-- always attempted under commercial pressure.
-- ---------------------------------------------------------------------------

CREATE TABLE tenant (
    id          bigserial   PRIMARY KEY,
    slug        text        NOT NULL UNIQUE,
    name        text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site (
    id          bigserial   PRIMARY KEY,
    tenant_id   bigint      NOT NULL REFERENCES tenant,
    name        text        NOT NULL,
    timezone    text        NOT NULL DEFAULT 'Asia/Kolkata',
    UNIQUE (tenant_id, name)
);

-- ---------------------------------------------------------------------------
-- assets
-- ---------------------------------------------------------------------------

CREATE TABLE gateway (
    id          bigserial   PRIMARY KEY,
    tenant_id   bigint      NOT NULL REFERENCES tenant,
    site_id     bigint      NOT NULL REFERENCES site,
    label       text        NOT NULL,
    host        text        NOT NULL,
    port        integer     NOT NULL DEFAULT 502,
    timeout_ms  integer     NOT NULL DEFAULT 2000,
    gap_ms      integer     NOT NULL DEFAULT 25,
    enabled     boolean     NOT NULL DEFAULT true,
    UNIQUE (tenant_id, host, port)
);

-- A datasheet revision that moves a register is a NEW row, never an edit,
-- so that readings already stored stay decodable. The register map itself
-- lives in code, in the product package that sells the instrument -
-- jetpace_environment.registers for the pressure transmitters - and is
-- pinned to the datasheet by tests; this table records which map a device
-- was read with.
CREATE TABLE device_model (
    id              bigserial PRIMARY KEY,
    code            text      NOT NULL,
    revision        text      NOT NULL,
    vendor          text      NOT NULL,
    kind            text      NOT NULL
                    CHECK (kind IN ('energy','pressure','temperature','humidity',
                                    'particle','gas','flow','velocity')),
    datasheet_ref   text      NOT NULL,
    UNIQUE (code, revision)
);

CREATE TABLE parameter (
    id          smallserial PRIMARY KEY,
    key         text        NOT NULL UNIQUE,
    label       text        NOT NULL,
    unit        text        NOT NULL,
    -- 'counter'       only ever increases; consumption is last - first
    -- 'instantaneous' a spot value; summarise with min/max/avg
    aggregation text      NOT NULL CHECK (aggregation IN ('counter','instantaneous')),
    decimals    smallint    NOT NULL DEFAULT 2
);

CREATE TABLE device (
    id              bigserial PRIMARY KEY,
    tenant_id       bigint    NOT NULL REFERENCES tenant,
    site_id         bigint    NOT NULL REFERENCES site,
    gateway_id      bigint    NOT NULL REFERENCES gateway,
    model_id        bigint    NOT NULL REFERENCES device_model,
    -- stable forever; readings are stored against id, never against code
    code            text      NOT NULL,
    name            text      NOT NULL,
    unit_id         smallint  NOT NULL,          -- Modbus slave address
    -- energy balance tree for meters; room cascade for pressure
    parent_id       bigint    REFERENCES device(id),
    ct_ratio        numeric(10,4) NOT NULL DEFAULT 1,
    pt_ratio        numeric(10,4) NOT NULL DEFAULT 1,
    scan_class      text      NOT NULL DEFAULT 'instantaneous'
                    CHECK (scan_class IN ('counter','instantaneous','critical')),
    enabled         boolean   NOT NULL DEFAULT true,
    commissioned_at timestamptz,
    -- a device removed from the sheet is switched off, not deleted:
    -- it stops being polled and drops out of reports, but its history stays
    retired_at      timestamptz,
    UNIQUE (tenant_id, code),
    UNIQUE (gateway_id, unit_id)
);

-- Per-device customisation lives here, not in the model: two devices of the
-- same model at the same site may expose different parameters and carry
-- different scaling.
CREATE TABLE device_parameter (
    device_id   bigint   NOT NULL REFERENCES device ON DELETE CASCADE,
    param       smallint NOT NULL REFERENCES parameter,
    enabled     boolean  NOT NULL DEFAULT true,
    scale       numeric(12,6),      -- overrides the model's scale
    alias       text,               -- the site's own name for it
    PRIMARY KEY (device_id, param)
);

-- ---------------------------------------------------------------------------
-- readings
-- ---------------------------------------------------------------------------

CREATE TABLE reading (
    ts         timestamptz      NOT NULL,
    device_id  bigint           NOT NULL,
    param      smallint         NOT NULL,
    value      double precision NOT NULL,
    -- 0 good | 1 substituted (backfilled from a device's own log) | 2 suspect
    quality    smallint         NOT NULL DEFAULT 0
);

SELECT create_hypertable('reading', 'ts', chunk_time_interval => INTERVAL '1 day');

-- Store-and-forward replays deliver at least once, so the write must be
-- idempotent. This unique index is what makes ON CONFLICT DO NOTHING work.
CREATE UNIQUE INDEX reading_pk ON reading (device_id, param, ts DESC);

ALTER TABLE reading SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id, param',
    timescaledb.compress_orderby   = 'ts DESC'
);

SELECT add_compression_policy('reading', INTERVAL '7 days');

INSERT INTO schema_migrations (version, name) VALUES (1, '0001_init')
ON CONFLICT (version) DO NOTHING;

COMMIT;
