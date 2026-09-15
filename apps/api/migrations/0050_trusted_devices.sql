-- 0050_trusted_devices.sql
-- "Remember this device for 7 days" (IAM-011 scoping - see
-- docs/decisions/ADR-061-trusted-devices.md). A trusted_device row is issued
-- ONLY after a session has already cleared MFA once (mfa_totp_verify,
-- mfa_passkey_verify_finish) - it is proof that this browser already proved
-- a second factor, not a substitute for proving one. login() consults it to
-- decide whether a NEW session may start pre-verified; it never lets
-- anyone skip enrolling a factor in the first place.
--
-- Same shape as sessions (0003) and user_totp_credential (0006): only a
-- SHA-256 hash of the bearer token is stored, the raw value is returned to
-- the caller exactly once (at issuance) and is not recoverable from this
-- table afterward. No DELETE grant, consistent with the rest of the auth
-- schema - revocation sets revoked_at, matching sessions/passkeys/TOTP.
--
-- Down (documented rather than shipped as a file, matching every earlier
-- migration here; run inside one transaction):
--   drop table trusted_device;
-- Safe to run while the old application version is serving: nothing older
-- than this change reads or writes this table.

begin;

create table trusted_device (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references users(id),
    token_hash      text not null,
    -- Client-supplied at issuance time (typically derived from the
    -- browser/OS), shown back in the device list (IAM-017's sibling for
    -- this table) so a person can tell which entry to revoke. Never
    -- required to be unique or trustworthy - it is a label, not an
    -- identifier.
    name            text,
    created_at      timestamptz not null default now(),
    last_used_at    timestamptz not null default now(),
    expires_at      timestamptz not null,
    revoked_at      timestamptz
);

create index trusted_device_user_id_idx on trusted_device(user_id);
-- Every active token must be look-up-able by its hash alone (that is the
-- only thing check() is given); a revoked or expired row does not need to
-- stay unique since login() only ever accepts a live one, but the column
-- has no legitimate reason to collide even so - two independently
-- generated 256-bit tokens colliding is not a case worth planning for.
create unique index trusted_device_token_hash_idx on trusted_device(token_hash);

alter table trusted_device owner to ledgr_migrator;

-- No DELETE grant, consistent with the rest of the auth schema.
grant select, insert, update on trusted_device to ledgr_app;

commit;
