-- 0076_google_link_ceremony.sql
-- IAM-010c: a Google sign-in whose verified email matches an existing account
-- created another way can now be linked, by proving that account's password.
-- See docs/decisions/ADR-105-google-sign-in-links-an-existing-account.md.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
-- auth_ceremony gains the 'google_link' kind and one nullable column,
-- google_link_identity. When login_google_callback resolves a verified Google
-- identity to GoogleSignInLinkRequired, it now stores that identity (subject,
-- email, name, picture, amr) here, server-side, against the existing user's id,
-- and hands the browser only the row id as a ticket. The follow-up request
-- (POST /v1/auth/login/google/link) sends the ticket and the existing account's
-- password; the identity it links is read back from this row, never from the
-- client - a client cannot name a Google subject of its own choosing.
--
-- jsonb rather than five columns: nothing queries inside it, it is written once
-- and read back once by api.auth.ceremony, and no other kind uses it.
--
-- auth_ceremony_google_link_has_identity mirrors 0046's
-- auth_ceremony_kind_has_matching_fields: a google_link row always carries an
-- identity and a user, and no other kind carries an identity. Every existing
-- row satisfies it (none is google_link, none has the new column set), so it
-- validates immediately.
--
-- Backward compatible: the column is nullable and unread by older code, and
-- the widened kind list only adds a value. Safe to apply while the previous
-- application version is serving.
--
-- Down (documented rather than shipped as a file, matching every earlier
-- migration here; run inside one transaction, after the application no longer
-- issues google_link ceremonies):
--   delete from auth_ceremony where kind = 'google_link';   -- as ledgr_ops (no delete grant on ledgr_app)
--   alter table auth_ceremony drop constraint auth_ceremony_google_link_has_identity;
--   alter table auth_ceremony drop column google_link_identity;
--   alter table auth_ceremony drop constraint auth_ceremony_kind_check;
--   alter table auth_ceremony add constraint auth_ceremony_kind_check
--       check (kind in ('passkey_registration', 'passkey_authentication',
--                       'google_oidc', 'google_signup', 'email_verification'));

begin;

alter table auth_ceremony add column google_link_identity jsonb;

comment on column auth_ceremony.google_link_identity is 'IAM-010c: the verified Google identity (subject, email, name, picture, amr) a google_link ceremony will link to user_id once that account''s password is proven. NULL for every other kind.';

alter table auth_ceremony drop constraint auth_ceremony_kind_check;
alter table auth_ceremony add constraint auth_ceremony_kind_check
    check (kind in (
        'passkey_registration',
        'passkey_authentication',
        'google_oidc',
        'google_signup',
        'email_verification',
        'google_link'
    ));

alter table auth_ceremony add constraint auth_ceremony_google_link_has_identity
    check ((kind = 'google_link') = (google_link_identity is not null and user_id is not null));

commit;
