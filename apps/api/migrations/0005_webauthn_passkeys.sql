-- 0005_webauthn_passkeys.sql
-- WebAuthn passkeys (IAM-010): a first-class sign-in method, on equal
-- footing with Google and email+password - not merely a second factor.
-- Multiple passkeys per user, each named for the user's own reference and
-- individually revocable. See
-- docs/decisions/ADR-007-webauthn-passkeys.md.
--
-- No row-level security, for the same reason as users/user_password_
-- credential/sessions/user_google_identity (0003, 0004): this is global
-- identity data, not tenant data, and there is no organization_id/
-- administration_id predicate that applies to it.

begin;

create table user_passkey (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null references users(id),
    name                text not null,
    -- The WebAuthn credential ID. Globally unique by construction (it's
    -- generated with enough entropy by the authenticator itself) and MUST
    -- be looked up without a user_id filter during authentication -
    -- passkeys are discoverable/usernameless by design (see the ADR): the
    -- server does not know which user is authenticating until this
    -- column resolves it.
    credential_id       bytea not null unique,
    public_key          bytea not null,
    sign_count          bigint not null default 0,
    transports          text[],
    aaguid              text,
    -- "Backup eligible" = this credential CAN be synced (e.g. iCloud
    -- Keychain, Google Password Manager); "backed up" = it currently IS.
    -- Recorded for future risk-signal use (a synced passkey is a
    -- different trust profile than one bound to a single hardware
    -- device) - nothing in this change acts on it yet.
    backup_eligible     boolean not null default false,
    backed_up           boolean not null default false,
    created_at          timestamptz not null default now(),
    last_used_at        timestamptz,
    -- Individually revocable (this task's requirement): revoking one
    -- passkey sets only its own revoked_at, never touching any other row
    -- - see WebAuthnService.revoke_passkey and its ownership check.
    revoked_at          timestamptz,
    constraint user_passkey_sign_count_non_negative check (sign_count >= 0),
    constraint user_passkey_name_not_blank check (length(trim(name)) > 0)
);

create index user_passkey_user_id_idx on user_passkey(user_id);

alter table user_passkey owner to ledgr_migrator;

-- No DELETE grant, consistent with the rest of the auth schema: a
-- revoked passkey is kept (revoked_at set), not removed, so a user's
-- passkey list shows their full history, not just what's currently live.
grant select, insert, update on user_passkey to ledgr_app;

commit;
