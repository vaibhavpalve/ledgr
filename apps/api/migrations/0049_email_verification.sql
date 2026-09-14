-- 0049_email_verification.sql
-- IAM-010b: e-mail verification for password signup - the gap ADR-054 named
-- and deliberately left open ("adding verification later needs a
-- users.email_verified_at column and a gate on it, nothing this change would
-- have to unwind"). See docs/decisions/ADR-060-session-backed-tenant-context.md.
--
-- ===========================================================================
-- Two small things, both backward compatible and both reversible
-- ===========================================================================
--
-- 1. users.email_verified_at - nullable, no default. NULL is the real state
--    every existing password account is in (nothing ever verified them) and
--    the state a new password signup starts in; it is not an error, and no
--    row is backfilled to pretend otherwise. A Google-created account is
--    stamped by the application at creation, because the identity provider
--    verified the address before LEDGR saw it (IAM-010b's own exemption).
--    Code that predates this column keeps working: it never selected it.
--
-- 2. auth_ceremony gains the 'email_verification' kind, widened exactly the
--    way 0047 widened it for google_signup and for the same reason: the
--    single-use link token is the same shape as the signup ticket (one row,
--    issued once, consumed once, a user_id and nothing else) and the existing
--    auth_ceremony_kind_has_matching_fields constraint already permits a kind
--    that sets neither the WebAuthn nor the Google-OIDC column group.
--
-- Down (documented rather than shipped as a file, matching every earlier
-- migration here; run inside one transaction):
--   alter table auth_ceremony drop constraint auth_ceremony_kind_check;
--   alter table auth_ceremony add constraint auth_ceremony_kind_check
--       check (kind in ('passkey_registration', 'passkey_authentication',
--                       'google_oidc', 'google_signup'));
--   alter table users drop column email_verified_at;
-- Both are safe to run while the old application version is serving: the
-- column is nullable and unread by anything older than this change.

begin;

alter table users add column email_verified_at timestamptz;

comment on column users.email_verified_at is 'IAM-010b: when this address was proven to belong to the person - by a verification link, or by Google at sign-in. NULL is unverified: may sign in and onboard, may not post to the ledger (api.auth.email_verification.require_verified_email).';

alter table auth_ceremony drop constraint auth_ceremony_kind_check;
alter table auth_ceremony add constraint auth_ceremony_kind_check
    check (kind in (
        'passkey_registration',
        'passkey_authentication',
        'google_oidc',
        'google_signup',
        'email_verification'
    ));

-- The resend path retires a user's earlier unconsumed links before issuing a
-- new one (UPDATE ... WHERE kind = 'email_verification' AND user_id = ...).
-- 0046's auth_ceremony_user_idx already covers that predicate.

commit;
