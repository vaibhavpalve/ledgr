-- 0006_mfa.sql
-- MFA foundation (IAM-011, IAM-012, IAM-010e). See
-- docs/decisions/ADR-008-mfa-policy.md.
--
-- Three changes:
--   1. user_totp_credential - the TOTP factor. Reuses the SAME
--      KeyManagementService abstraction as document encryption
--      (api.crypto.kms, SEC-022) rather than a separate static-key
--      scheme, per SEC-024's requirement for field-level encryption of
--      authentication secrets: a fresh DEK is generated per credential,
--      wrapped by the KMS, and only the wrapped form is stored - the raw
--      TOTP secret is never persisted.
--   2. sessions.mfa_verified_at - records that a session's second factor
--      was actually verified, not merely that the user has one enrolled.
--      "Mandatory... no opt-out" means checked, not merely available.
--   3. user_passkey.authenticator_attachment - captures whether a passkey
--      is a platform (device-bound) or cross-platform (roaming/synced)
--      authenticator. IAM-012 names both "passkey" and "platform
--      biometrics bound to a device key" as acceptable factors; this
--      change records the distinction for audit purposes even though
--      both are treated identically for MFA-satisfaction purposes today
--      (see ADR-008) - any non-revoked passkey counts, regardless of
--      attachment.

begin;

-- ---------------------------------------------------------------------------
-- user_totp_credential
-- ---------------------------------------------------------------------------
-- At most one ACTIVE (non-revoked) TOTP credential per user - unlike
-- passkeys, TOTP is conventionally a single enrolled authenticator app,
-- not a set of named, individually revocable devices. This is a partial
-- unique index (WHERE revoked_at IS NULL), not a plain UNIQUE(user_id),
-- for the same reason administration_encryption_key (0002) uses one: a
-- plain UNIQUE would permanently block re-enrollment after a revoke,
-- since the constraint can't distinguish "this user already has an
-- active credential" from "this user has ONE OLD REVOKED credential in
-- their history."
--
-- wrapped_secret/nonce is the TOTP base32 seed, AES-256-GCM encrypted;
-- wrapped_dek/wrap_algorithm/kek_key_id is that encryption's own key,
-- wrapped by the KMS - the same envelope-encryption shape as
-- administration_encryption_key (0002), scoped to one row instead of one
-- administration since this secret belongs to a user, not a tenant.
create table user_totp_credential (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null references users(id),
    wrapped_secret      bytea not null,
    secret_nonce        bytea not null,
    wrapped_dek         bytea not null,
    wrap_algorithm      text not null check (wrap_algorithm in ('RSA-OAEP-256', 'A256KW')),
    kek_key_id          text not null,
    -- Replay protection: a submitted code's matched time-step must exceed
    -- this value or it is rejected, even if the code is otherwise
    -- correct - see api.auth.totp.TotpService.verify_code. Null until the
    -- first successful verification.
    last_used_step      bigint,
    -- Enrollment is not "live" as an MFA factor until confirmed with a
    -- valid code (proves the secret was actually captured correctly by
    -- the user's authenticator app) - null means pending.
    confirmed_at        timestamptz,
    created_at          timestamptz not null default now(),
    revoked_at          timestamptz,
    constraint user_totp_credential_last_used_step_non_negative
        check (last_used_step is null or last_used_step >= 0)
);

create index user_totp_credential_user_id_idx on user_totp_credential(user_id);
create unique index user_totp_credential_one_active_idx
    on user_totp_credential(user_id)
    where revoked_at is null;

alter table user_totp_credential owner to ledgr_migrator;

-- No DELETE grant, consistent with the rest of the auth schema.
grant select, insert, update on user_totp_credential to ledgr_app;

-- ---------------------------------------------------------------------------
-- sessions.mfa_verified_at
-- ---------------------------------------------------------------------------
alter table sessions add column mfa_verified_at timestamptz;

-- ---------------------------------------------------------------------------
-- user_passkey.authenticator_attachment
-- ---------------------------------------------------------------------------
alter table user_passkey add column authenticator_attachment text
    check (authenticator_attachment in ('platform', 'cross-platform'));

commit;
