-- 0047_google_initiated_signup.sql
-- FR-MDL-001/IAM-010: a Google identity that matches no existing account
-- can now finish signup, not just be told to sign up with a password first.
-- See docs/decisions/ADR-054-signup-and-login.md's addendum and
-- api.auth.ceremony's module docstring.
--
-- ===========================================================================
-- Why this is a widened CHECK constraint, not a new table
-- ===========================================================================
--
-- api.auth.google_signin.GoogleSignInService.sign_in already creates a bare
-- `users` row and links the Google identity when neither the subject nor the
-- email match anything (see that module - unchanged by this migration). What
-- was missing was a way to hand that bare user's id back to a follow-up
-- request that asks FR-MDL-001's one question and creates the organization -
-- Google's own OAuth redirect has no room to carry extra fields through it.
--
-- `auth_ceremony` (migration 0046) already exists for exactly this shape of
-- problem (short-lived, single-use, one row per attempt) and already has a
-- `user_id` column with nothing else this new kind needs. A `google_signup`
-- ceremony sets only `kind` and `user_id`; `webauthn_challenge` and the three
-- `google_*` columns all stay null, which the existing
-- `auth_ceremony_kind_has_matching_fields` constraint already permits without
-- modification - neither of its two equivalences names `google_signup`, so
-- both sides evaluate to false for it, which is exactly "no extra columns
-- required." Only the column's own allow-list of `kind` values needs
-- widening, which is what this migration does.

begin;

alter table auth_ceremony drop constraint auth_ceremony_kind_check;
alter table auth_ceremony add constraint auth_ceremony_kind_check
    check (kind in (
        'passkey_registration', 'passkey_authentication', 'google_oidc', 'google_signup'
    ));

commit;
