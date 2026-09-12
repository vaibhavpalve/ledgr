-- 0003_authentication.sql
-- Authentication foundation: user records, password credential storage, and
-- sessions. Implements IAM-013 (password policy at the schema/storage
-- level) and IAM-016 (session lifetime). See
-- docs/decisions/ADR-005-authentication-foundation.md for the design
-- reasoning — stateful sessions over JWTs, Argon2id, and what is
-- deliberately NOT built here (login endpoint, MFA enrollment, Google
-- OAuth, passkeys, account recovery — all separate IAM-010/012/014/018
-- items).
--
-- This migration also closes out the deferred user foreign keys left as
-- bare `uuid` columns in 0001 and 0002 ("FK to users table, added when
-- users ships") — that's now.

begin;

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------
-- Global identity (PRD §15: "User | id, identity, MFA state, status |
-- Global identity, tenant-scoped access"). Deliberately has NO
-- organization_id/administration_id — a user is not tenant data, and at
-- the moment of authentication (looking a user up by email, validating a
-- session token) there IS no tenant context yet; that's what
-- authentication establishes the *prerequisite* for. Tenant-scoped access
-- itself is a RoleAssignment concept (IAM-030+, not yet built) layered on
-- top of this global identity, not part of it.
--
-- No row-level security on this table or the two below, for the same
-- reason: RLS as used elsewhere in this schema (app.has_administration_access)
-- has no meaning before tenant context exists. Least-privilege here is
-- enforced by which queries the application ever issues (only
-- apps/api/src/api/auth/ touches these tables) and by ordinary GRANTs, not
-- by a tenant predicate that doesn't apply.
create extension if not exists citext;

create table users (
    id                  uuid primary key default gen_random_uuid(),
    email               citext not null unique,
    status              text not null default 'active'
                            check (status in ('active', 'suspended', 'deactivated')),
    mfa_enrolled        boolean not null default false,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- user_password_credential
-- ---------------------------------------------------------------------------
-- One password credential per user (IAM-010d allows multiple sign-in
-- *methods* — Google, passkey, password — but at most one of each; this
-- table is the password method specifically). Storage only: password
-- policy (length, no composition rules, no forced rotation, breach
-- screening) is enforced in application code before a row here is ever
-- written — see apps/api/src/api/auth/passwords.py — because none of that
-- is expressible as a column constraint against a value the database never
-- sees in plaintext.
create table user_password_credential (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null unique references users(id),
    password_hash       text not null,
    algorithm           text not null default 'argon2id',
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- sessions
-- ---------------------------------------------------------------------------
-- IAM-016: 12-hour absolute maximum lifetime (expires_at, fixed at
-- issuance), 30-minute idle timeout for privileged roles (checked against
-- last_active_at on every validation), absolute re-authentication for
-- sensitive actions (last_reauthenticated_at, a primitive future
-- sensitive-action endpoints can check — no such endpoints exist yet).
--
-- `privileged` is supplied by the caller at issuance, not derived here:
-- this schema has no role/permission model yet (IAM-030+), so "is this a
-- privileged session" is a question this table can record an answer to but
-- not itself compute. Until RoleAssignment exists, callers should default
-- to true (the safer, tighter-timeout choice) rather than false.
--
-- token_hash, not the raw bearer token: the same principle as password
-- storage — the secret a client presents is never stored in recoverable
-- form, only its SHA-256 hash (a random 256-bit token has enough entropy
-- that a fast hash is appropriate here, unlike a user-chosen password).
create table sessions (
    id                          uuid primary key default gen_random_uuid(),
    user_id                     uuid not null references users(id),
    token_hash                  text not null unique,
    privileged                  boolean not null default true,
    created_at                  timestamptz not null default now(),
    expires_at                  timestamptz not null,
    last_active_at              timestamptz not null default now(),
    last_reauthenticated_at     timestamptz not null default now(),
    revoked_at                  timestamptz,
    ip_address                  inet,
    user_agent                  text,
    constraint sessions_expires_after_created check (expires_at > created_at)
);

create index sessions_user_id_idx on sessions(user_id);
-- Supports a future cleanup job purging long-expired/revoked rows; no such
-- job exists yet (session rows are not fiscal records — the 7-year
-- retention discipline elsewhere in this schema does not apply to them).
create index sessions_expires_at_idx on sessions(expires_at);

-- ---------------------------------------------------------------------------
-- Ownership and grants
-- ---------------------------------------------------------------------------
alter table users                    owner to ledgr_migrator;
alter table user_password_credential owner to ledgr_migrator;
alter table sessions                 owner to ledgr_migrator;

-- No DELETE grant on any of the three, consistent with the rest of this
-- schema: users are deactivated (status column), not removed; credentials
-- are replaced by updating password_hash in place, not by deleting and
-- reinserting; sessions are revoked (revoked_at), not deleted, so IAM-017's
-- "device sessions listed... individually revocable" has a full history to
-- show, not just currently-live ones.
grant select, insert, update on users                    to ledgr_app;
grant select, insert, update on user_password_credential to ledgr_app;
grant select, insert, update on sessions                 to ledgr_app;

-- ---------------------------------------------------------------------------
-- Close out the deferred foreign keys from 0001 and 0002
-- ---------------------------------------------------------------------------
alter table firm_engagement
    add constraint firm_engagement_invited_by_user_id_fkey
    foreign key (invited_by_user_id) references users(id);
alter table firm_engagement
    add constraint firm_engagement_accepted_by_user_id_fkey
    foreign key (accepted_by_user_id) references users(id);
alter table firm_engagement
    add constraint firm_engagement_revoked_by_user_id_fkey
    foreign key (revoked_by_user_id) references users(id);

alter table period
    add constraint period_locked_by_user_id_fkey
    foreign key (locked_by_user_id) references users(id);

alter table administration_encryption_key
    add constraint administration_encryption_key_revoked_by_user_id_fkey
    foreign key (revoked_by_user_id) references users(id);

commit;
