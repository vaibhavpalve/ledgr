-- 0046_signup_and_ceremonies.sql
-- IAM-010, FR-MDL-001, FR-ONB-001a/001b (PRD §5.1, §6.1, §8.2). See
-- docs/decisions/ADR-054-signup-and-login.md.
--
--   FR-MDL-001    Signup asks one question - own business or clients - and
--                 branches into Model B (self-managed) or Model A (firm).
--   FR-ONB-001b   Firm signup path additionally captures firm name and KvK
--                 number, landing on an empty client portfolio.
--   IAM-010       Google, passkey and email+password, offered side by side.
--
-- ===========================================================================
-- What this migration adds, and why it is two small things rather than one
-- ===========================================================================
--
-- 1. app.signup_firm_organization() - the firm-branch twin of migration
--    0001's app.signup_self_managed_organization(). That function only ever
--    creates kind='business'; FR-ONB-001b's firm branch needs kind='firm',
--    and there is no other function anywhere that can produce one - reading
--    0001 in full before writing this confirmed the gap rather than assumed
--    it. Same shape, same SECURITY DEFINER/ledgr_bootstrap ownership, same
--    reasoning: creating an organization is the one moment nothing about
--    tenancy exists yet for an ordinary RLS-scoped INSERT to apply to.
--
-- 2. auth_ceremony - short-lived, single-use state for the two sign-in
--    methods whose protocol requires a round trip before the caller is
--    known: WebAuthn (a challenge issued by begin_registration/
--    begin_authentication, per api.auth.passkeys' own docstring: "ceremony-
--    in-progress state is NOT persisted by this module - a server-side flow
--    record, not a client-readable cookie") and Google OIDC (the `state`/
--    `nonce`/PKCE `code_verifier` issued by GoogleOidcClient.start_sign_in,
--    per api.auth.google_oidc's own docstring, same reasoning). Both
--    modules were built with this storage decision deliberately left to
--    "there is no login HTTP endpoint yet to own it" - this is that
--    decision, made once, for both.
--
-- One shared table rather than two, because both are the same shape (issued
-- once, read back once, expire quickly, never reused) and a single cleanup
-- policy (TTL past expires_at) covers both. `kind` keeps the two from being
-- confused with each other; the columns each kind does not use stay null.

begin;

-- ---------------------------------------------------------------------------
-- app.signup_firm_organization
-- ---------------------------------------------------------------------------
create or replace function app.signup_firm_organization(
    p_name text,
    p_kvk_number text
) returns organization
language plpgsql
security definer
set search_path = public, app
as $$
declare
    v_org organization;
begin
    insert into organization (kind, name, kvk_number, claimed_at)
    values ('firm', p_name, p_kvk_number, now())
    returning * into v_org;
    return v_org;
end;
$$;

alter function app.signup_firm_organization(text, text) owner to ledgr_bootstrap;
revoke all on function app.signup_firm_organization(text, text) from public;
grant execute on function app.signup_firm_organization(text, text) to ledgr_app;

-- ---------------------------------------------------------------------------
-- auth_ceremony
-- ---------------------------------------------------------------------------
-- No organization_id/administration_id: like `users` and `sessions`
-- (ADR-005), this is global identity/authentication plumbing that exists
-- before any tenant is known, not tenant data - the same reasoning, not an
-- oversight, and RLS with no meaningful predicate would just be a blanket
-- allow. Least-privilege here is "only api.auth.* code ever queries this
-- table," the same posture ADR-005 states for users/sessions.
create table auth_ceremony (
    id              uuid primary key default gen_random_uuid(),
    kind            text not null check (
                        kind in ('passkey_registration', 'passkey_authentication', 'google_oidc')
                    ),

    -- WebAuthn: the challenge issued by begin_registration/
    -- begin_authentication, echoed back through complete_* on the matching
    -- request. NULL for google_oidc.
    webauthn_challenge bytea,

    -- Google OIDC: `state` is also this ceremony's own lookup key from the
    -- callback (Google echoes it back verbatim in the query string, so the
    -- callback handler finds its row by state, not by id). `nonce` and
    -- code_verifier are GoogleOidcClient.complete_sign_in's other two
    -- required inputs. NULL for the two passkey kinds.
    google_state        text,
    google_nonce         text,
    google_code_verifier text,

    -- Which user this ceremony is FOR, where known in advance:
    --   passkey_registration   always known - a signed-in user (enrolling
    --                          MFA, or adding a second passkey) is
    --                          registering a credential for themselves.
    --   passkey_authentication NULL - IAM-010's "click passkey, no email
    --                          typed first" usernameless flow resolves the
    --                          user from the credential in the response,
    --                          not from this row.
    --   google_oidc            NULL - the identity is not known until
    --                          Google's callback returns it.
    user_id         uuid references users(id),

    created_at      timestamptz not null default now(),
    -- Five minutes: long enough for a person to complete a WebAuthn
    -- ceremony or a Google consent redirect, short enough that a leaked or
    -- abandoned row is not a standing replay opportunity.
    expires_at      timestamptz not null default (now() + interval '5 minutes'),
    -- Set the moment a ceremony is consumed, so a completion request that
    -- somehow arrives twice (a doubled network request, not a security
    -- boundary this column is the only defense for - the underlying
    -- WebAuthn/OIDC verification is) finds nothing usable the second time.
    consumed_at     timestamptz,

    constraint auth_ceremony_kind_has_matching_fields check (
        (kind in ('passkey_registration', 'passkey_authentication'))
            = (webauthn_challenge is not null)
        and
        (kind = 'google_oidc')
            = (google_state is not null and google_nonce is not null
               and google_code_verifier is not null)
    )
);

create unique index auth_ceremony_google_state_idx
    on auth_ceremony(google_state) where google_state is not null;

-- Not a foreign key from anywhere, and not queried by user_id in the hot
-- path (passkey_authentication and google_oidc are looked up by id/state,
-- not by who they turn out to belong to) - this index exists only so an
-- account-deletion or support-tooling sweep can find a user's own
-- in-flight ceremonies without a full scan.
create index auth_ceremony_user_idx on auth_ceremony(user_id) where user_id is not null;

alter table auth_ceremony owner to ledgr_migrator;
grant select, insert, update on auth_ceremony to ledgr_app;
-- No delete grant: expired rows are inert (every read path checks
-- expires_at/consumed_at) and a periodic purge, if one is ever needed, is
-- ops work against ledgr_ops - the same posture migrations 0022/0008 take
-- on their own short-lived rows (idempotency keys, auth attempts).

commit;
