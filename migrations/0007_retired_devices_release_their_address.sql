-- 0007_retired_devices_release_their_address.sql
--
-- A bug found by commissioning a device through the form, which no test
-- caught because no test had ever retired a device and then reused its
-- slave address.
--
-- 0001 declares UNIQUE (gateway_id, unit_id) on `device`, with no
-- condition. 0001 also says, two lines above it, that "a device removed
-- from the sheet is switched off, not deleted: it stops being polled and
-- drops out of reports, but its history stays". Those two statements
-- disagree. An unconditional unique index means a RETIRED device goes on
-- holding its Modbus address on that bus forever, so replacing a failed
-- transmitter - take the old one out, put a new one in at the same slave
-- address, which is the entire point of a slave address being set with a
-- switch on the instrument - cannot be done at all.
--
-- Every query in the application already reads it the other way: each one
-- filters `retired_at IS NULL`, and so does the readable duplicate check
-- in web/writes.py. The result was a commissioning form that said the
-- address was free, followed by a 500 and a UniqueViolation in the log.
--
-- A PARTIAL unique index says what was meant: one live device per address
-- per bus, and a retired one is not a live device. Two devices answering
-- to one address on an RS-485 pair collide and neither reads reliably, so
-- the constraint is worth having - it just has to be about the devices
-- that are actually on the bus.
--
-- THE CODE IS DELIBERATELY NOT TREATED THE SAME WAY. UNIQUE (tenant_id,
-- code) stays unconditional, because readings are stored against a device
-- id and a code is how a report names it: reusing a retired code would
-- put a new instrument's readings on a page that also shows the old one's
-- history, with nothing to separate them. An address is a position on a
-- bus and is meant to be reused; a code is an identity and is not.

BEGIN;

ALTER TABLE device DROP CONSTRAINT IF EXISTS device_gateway_id_unit_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS device_live_address
    ON device (gateway_id, unit_id)
    WHERE retired_at IS NULL;

COMMENT ON INDEX device_live_address IS
    'One LIVE device per Modbus address per gateway. Partial, because a '
    'retired device is off the bus and its address is free for the '
    'instrument that replaced it.';

INSERT INTO schema_migrations (version, name)
VALUES (7, '0007_retired_devices_release_their_address')
ON CONFLICT (version) DO NOTHING;

COMMIT;
