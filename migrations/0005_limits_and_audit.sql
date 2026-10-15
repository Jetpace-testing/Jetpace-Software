-- 0005_limits_and_audit.sql
--
-- Two tables that have to exist before a customer can commission a device
-- from the dashboard, and that nothing before this migration could do
-- without them.
--
--   device_limit  the low and high set points for one parameter on one
--                 device - LSP and HSP on the commissioning form. Until
--                 now there was nowhere to put them, which is why five
--                 things the Figma design draws are not built: the Active
--                 Alarms figure, the severity column, the Recent Alarms
--                 panel, the Acknowledge action, and the threshold lines
--                 on the trend. All five need a limit to compare against.
--                 This is the first half of architecture.md §10 step 6.
--
--   audit         who did what. The application has had no write path at
--                 all, so there has been nothing to record; the device
--                 form is the first. A write path without an audit trail
--                 is worse at a validated site than no write path, because
--                 a device whose register map changed with no record of
--                 who changed it taints every batch record that cites it.
--
-- BOTH ARE APPEND-ONLY, like `reading`. The grant in setup_postgres.py
-- revokes UPDATE and DELETE on them from the application role, so the
-- shapes below have to work without ever rewriting a row:
--
--   A limit is never edited. Changing LSP from -5 to -8 INSERTS a second
--   row; the one in force is the most recent by set_at. That is not
--   bookkeeping for its own sake - a reading taken last month was either
--   inside the limit that applied last month or it was not, and an
--   excursion report that silently re-judges history against today's set
--   point is the exact failure 21 CFR Part 11 exists to prevent. It is
--   the same argument 0001 makes for keeping a register map as a new
--   device_model row rather than an edit.
--
--   An audit row is never corrected. A mistake is a new row that says so.
--
-- `set_by` and `actor` are deliberately nullable and are written NULL by
-- the dashboard today, because there is no user model yet (§10). A column
-- that exists and is honestly empty is better than one added later: it
-- means the first login does not need a migration, and it means nothing
-- can quietly attribute an action to a name it invented. A null here
-- reads as "this install had no users when it happened", which is true.

BEGIN;

-- ---------------------------------------------------------------------------
-- limits
-- ---------------------------------------------------------------------------

CREATE TABLE device_limit (
    id          bigserial        PRIMARY KEY,
    tenant_id   bigint           NOT NULL REFERENCES tenant,
    -- No ON DELETE CASCADE, deliberately, and unlike device_parameter.
    -- 0001 says a device removed from the sheet is switched off rather than
    -- deleted; this reference is what makes that more than a comment,
    -- because a device carrying limits now cannot be deleted at all
    -- without first deleting the record of what it was held to.
    device_id   bigint           NOT NULL REFERENCES device,
    param       smallint         NOT NULL REFERENCES parameter,
    -- LSP and HSP. Either may be absent: a room with a minimum cascade and
    -- no ceiling is an ordinary case, and so is a counter with a maximum
    -- and no floor. Both absent is not a limit, which the check below
    -- rejects rather than storing a row that means nothing.
    low         double precision,
    high        double precision,
    set_at      timestamptz      NOT NULL DEFAULT now(),
    set_by      text,                 -- null until there are users (§10)
    reason      text,
    CONSTRAINT device_limit_is_a_limit
        CHECK (low IS NOT NULL OR high IS NOT NULL),
    -- Strictly less than, not <=. Equal set points describe a band no
    -- reading can be inside, so every sample would be an excursion - a
    -- configuration mistake that is much cheaper to refuse here than to
    -- diagnose from a wall of alarms.
    CONSTRAINT device_limit_ordered
        CHECK (low IS NULL OR high IS NULL OR low < high)
);

-- The access pattern is "the limit in force for this device and parameter",
-- which is one row off the front of this index, and "every limit this
-- device has ever had", which is a scan of the same prefix.
CREATE INDEX device_limit_current
    ON device_limit (device_id, param, set_at DESC);

COMMENT ON TABLE device_limit IS
    'Append-only. The limit in force is the latest row by set_at; a change '
    'inserts, never updates, so history stays judgeable against the limit '
    'that actually applied at the time.';

-- ---------------------------------------------------------------------------
-- audit
-- ---------------------------------------------------------------------------

CREATE TABLE audit (
    id          bigserial   PRIMARY KEY,
    tenant_id   bigint      NOT NULL REFERENCES tenant,
    at          timestamptz NOT NULL DEFAULT now(),
    actor       text,                 -- null until there are users (§10)
    -- Dotted and coarse: 'device.add', 'device.retire', 'limit.set'. Coarse
    -- because the point is to be readable in a year by somebody who does
    -- not know this schema, and a constrained vocabulary in a CHECK would
    -- need a migration every time the application learns a new verb.
    action      text        NOT NULL,
    -- What it was done to, in the terms the operator used: a device code,
    -- not an id. TEXT and not a foreign key on purpose - an audit row has
    -- to outlive the thing it describes, including a row somebody later
    -- deletes with a superuser connection.
    subject     text        NOT NULL,
    -- Everything else, including the values as submitted. jsonb because
    -- the interesting fields differ per action and because this is read by
    -- a person far more often than it is queried.
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX audit_recent ON audit (tenant_id, at DESC);
CREATE INDEX audit_subject ON audit (subject, at DESC);

COMMENT ON TABLE audit IS
    'Append-only. A mistake is corrected by a new row saying so, never by '
    'editing one. actor is null on installs that have no user model yet.';

INSERT INTO schema_migrations (version, name)
VALUES (5, '0005_limits_and_audit')
ON CONFLICT (version) DO NOTHING;

COMMIT;
