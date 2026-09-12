-- 0004_google_identity.sql
-- Links a verified Google identity to a LEDGR user (IAM-010a, IAM-010b).
-- See docs/decisions/ADR-006-google-sign-in.md for the PKCE flow and the
-- reasoning behind the scope list and the email_verified rejection.
--
-- Keyed by google_subject (the OIDC `sub` claim, Google's own stable,
-- opaque identifier for the account) - NEVER by email. IAM-010c prohibits
-- automatically linking an incoming Google identity to an existing user by
-- email match alone ("it is an account takeover path"); keying this table
-- by subject instead means the database itself cannot express an
-- email-based link even if application code tried to construct one that
-- way - there is no email column here to join on for linking purposes,
-- only for audit ("what email did this identity present when it was
-- linked").

begin;

create table user_google_identity (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null unique references users(id),
    google_subject      text not null unique,
    email_at_link_time  citext not null,
    created_at          timestamptz not null default now()
);

create index user_google_identity_user_id_idx on user_google_identity(user_id);

alter table user_google_identity owner to ledgr_migrator;

-- No DELETE grant, consistent with the rest of the auth schema (0003):
-- removing a sign-in method is a future IAM-010d flow's job and should
-- leave an audit trail, not silently vanish a row.
grant select, insert on user_google_identity to ledgr_app;

commit;
