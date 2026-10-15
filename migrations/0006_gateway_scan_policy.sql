-- 0006_gateway_scan_policy.sql
--
-- Two columns the gateway table needs before the device list can leave
-- config/prototype.toml (debt #3).
--
-- The file describes four things about a bus: how long to wait for a
-- reply, how long to leave between requests, how often to walk the whole
-- device list, and how many passes to skip a device that is not
-- answering. 0001 stored the first two as timeout_ms and gap_ms and left
-- the other two behind, because nothing read the gateway table for
-- polling policy - the poller read the file.
--
-- They are per-gateway and not per-install, which is the reason they
-- belong here rather than on a command line. A bus of thirty devices at
-- 9600 baud cannot be walked as often as a bus of three at 38400, and on
-- a site with fifteen converters that is fifteen different answers.
--
-- Defaults match GatewaySpec's in poller.py, so a gateway row written
-- before this migration behaves exactly as the dataclass would have
-- defaulted it. config/prototype.toml says 2000 ms rather than 1000 for
-- the prototype's bus; seed_prototype.py carries that across, so the
-- live install keeps the interval it has been running on rather than
-- quietly doubling its read rate.

BEGIN;

ALTER TABLE gateway
    ADD COLUMN IF NOT EXISTS scan_interval_ms integer NOT NULL DEFAULT 1000,
    -- FIXED, not exponential, and the comment in poller.py explains why:
    -- a device that has been repaired must be noticed within a bounded
    -- time, and exponential backoff eventually retries so rarely that a
    -- working device stays marked dead for hours.
    ADD COLUMN IF NOT EXISTS offline_backoff_cycles integer NOT NULL DEFAULT 5;

ALTER TABLE gateway
    ADD CONSTRAINT gateway_scan_interval_sane
        CHECK (scan_interval_ms BETWEEN 100 AND 3600000),
    ADD CONSTRAINT gateway_backoff_sane
        CHECK (offline_backoff_cycles BETWEEN 0 AND 1000);

COMMENT ON COLUMN gateway.scan_interval_ms IS
    'How often to walk this bus end to end. One pass takes as long as it '
    'takes; this is the interval between the start of one and the next.';
COMMENT ON COLUMN gateway.offline_backoff_cycles IS
    'Passes to skip a device that did not answer. A dead device costs a '
    'full timeout every time it is tried, and on a shared bus that is '
    'time taken from the healthy devices behind it.';

INSERT INTO schema_migrations (version, name)
VALUES (6, '0006_gateway_scan_policy')
ON CONFLICT (version) DO NOTHING;

COMMIT;
