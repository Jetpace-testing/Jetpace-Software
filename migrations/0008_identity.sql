-- 0008_identity.sql
--
-- Who may do what, and who did it. Specification §10 (identity, roles and
-- audit) and §11 (GxP constraints), which between them make four
-- requirements this schema has to carry rather than a policy document:
-- unique accounts, no shared logins, a password policy, and automatic
-- logoff. 21 CFR 11.10(d) and EU GMP Annex 11 §12.
--
-- The audit trail is already here and already append-only for everyone
-- including administrators - 0005 - which is the other half of §11 and
-- the reason `actor` has been sitting nullable since. It stops being null
-- when this lands.
--
-- THREE DECISIONS WORTH THE WORDS.
--
-- A ROLE IS GRANTED PER SITE, not per user. §10: "Scope permissions by
-- site as well as by role. A group manager over four plants and an
-- electrician over one are the same role at different scope; modelling
-- that as two roles produces a permission matrix nobody can reason about
-- within a year." Every install today has exactly one site, so every user
-- gets exactly one row and the scoping does nothing. That is the same
-- argument 0001 makes for tenant_id: a column you do not need yet is
-- free, and retrofitting it is the refactor that gets attempted under
-- commercial pressure and goes wrong.
--
-- A USER IS DISABLED, NEVER DELETED, for the same reason a device is
-- retired rather than dropped. Audit rows name their actor as text so
-- they survive anything, but "who was R. Shah" still has to be
-- answerable in five years when somebody reads a batch record. The grant
-- in setup_postgres.py revokes DELETE on app_user to make that more than
-- an intention.
--
-- THE DATABASE NEVER HOLDS A USABLE TOKEN. `session.token_hash` is the
-- SHA-256 of the value in the browser's cookie, so a database dump - a
-- backup on a USB stick, a support export, this project's own
-- pg_dump - yields nothing anybody can sign in with. Taken from Meril
-- RTD, which does the same.

BEGIN;

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------

CREATE TABLE app_user (
    id                   bigserial   PRIMARY KEY,
    tenant_id            bigint      NOT NULL REFERENCES tenant,
    -- Case-insensitively unique per tenant, enforced by the index below.
    -- Stored as typed, because a person's name is theirs; compared folded,
    -- because "R.Shah" and "r.shah" signing in as different people is how
    -- an audit trail stops meaning anything.
    username             text        NOT NULL,
    full_name            text        NOT NULL,
    -- NULL means this account cannot sign in WITH A PASSWORD. Today that
    -- is only a disabled account; §10 also anticipates OIDC through
    -- Keycloak for sites that want Active Directory sign-in, where the
    -- password lives at the identity provider and never here.
    password_hash        text,
    password_changed_at  timestamptz,
    -- Set on an administrator-issued password so the holder must replace
    -- it before doing anything. The alternative is an administrator who
    -- knows a working password for somebody else's account, which defeats
    -- attribution entirely.
    must_change_password boolean     NOT NULL DEFAULT false,
    -- Lockout survives a restart, unlike an in-process counter. A process
    -- restart must not be a way to clear a lockout, because on a site
    -- that anybody can reach the supervisor, it would be.
    failed_logins        integer     NOT NULL DEFAULT 0,
    locked_until         timestamptz,
    last_login_at        timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text,
    disabled_at          timestamptz,
    disabled_by          text,
    disabled_reason      text,
    CONSTRAINT app_user_named CHECK (length(btrim(username)) > 0),
    CONSTRAINT app_user_has_a_person CHECK (length(btrim(full_name)) > 0)
);

CREATE UNIQUE INDEX app_user_name
    ON app_user (tenant_id, lower(username));

COMMENT ON TABLE app_user IS
    'Disabled, never deleted: an audit row five years old has to stay '
    'answerable. DELETE is revoked from the application role.';

-- ---------------------------------------------------------------------------
-- roles, granted per site
-- ---------------------------------------------------------------------------

CREATE TABLE user_role (
    user_id    bigint      NOT NULL REFERENCES app_user ON DELETE CASCADE,
    site_id    bigint      NOT NULL REFERENCES site,
    -- Exactly the seven in specification §10. A CHECK rather than a
    -- lookup table because the set is fixed by the specification and a
    -- new role is a decision somebody should have to make in a migration,
    -- with the permission matrix in front of them.
    --
    --   viewer         dashboards, trends, run reports
    --   operator       + acknowledge and shelve
    --   manager        + export, limits, targets, baselines
    --   engineer       + devices, register maps, scan classes
    --   administrator  everything, except altering the audit log
    --   qa             review and sign records; changes no configuration
    --   support        diagnostics, versions, logs - time-boxed, consented,
    --                  and no instrument data by default
    role       text        NOT NULL CHECK (role IN (
                   'viewer', 'operator', 'manager', 'engineer',
                   'administrator', 'qa', 'support')),
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by text,
    -- Specification §10 time-boxes the support role. NULL is "no expiry",
    -- which every other role uses.
    expires_at timestamptz,
    PRIMARY KEY (user_id, site_id, role)
);

CREATE INDEX user_role_site ON user_role (site_id, role);

-- ---------------------------------------------------------------------------
-- sessions
-- ---------------------------------------------------------------------------

CREATE TABLE session (
    -- SHA-256 of the token in the cookie. The token itself is never
    -- written down anywhere on this machine.
    token_hash   text        PRIMARY KEY,
    user_id      bigint      NOT NULL REFERENCES app_user ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- Slid forward by real interaction only. A POLL MUST NOT COUNT: this
    -- dashboard refreshes four panels every five seconds, so a session
    -- that treated its own background traffic as activity would never
    -- expire and the automatic logoff Annex 11 §12 requires would be
    -- decorative. See web/sessions.py.
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    -- For the audit trail and for "where am I signed in", not for
    -- authorisation. An address is not an identity.
    ip           text,
    user_agent   text,
    ended_at     timestamptz,
    ended_reason text
);

-- NOT `session_user`: that is a reserved word in PostgreSQL, alongside
-- current_user and current_role, and an unquoted index of that name is a
-- syntax error rather than a name clash - which is a confusing way to
-- find out.
CREATE INDEX session_by_user ON session (user_id)    WHERE ended_at IS NULL;
CREATE INDEX session_expiry  ON session (expires_at) WHERE ended_at IS NULL;

COMMENT ON COLUMN session.token_hash IS
    'SHA-256 of the cookie value. A database dump yields nothing anybody '
    'can sign in with.';

INSERT INTO schema_migrations (version, name) VALUES (8, '0008_identity')
ON CONFLICT (version) DO NOTHING;

COMMIT;
