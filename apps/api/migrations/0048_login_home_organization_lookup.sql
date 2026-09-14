begin;

-- ===========================================================================
-- Fixes a login-path bug found by actually exercising it end-to-end for the
-- first time against a live Postgres (2026-09-14): every returning-user
-- login — password, passkey, and Google alike — refused with
-- errors.no_organization, even for a user who had just completed MFA
-- enrollment and whose own access token carried a real organization_id.
--
-- --- Root cause ---
--
-- api.auth.routes._home_organization_id (login, login_passkey_finish and
-- login_google_callback all call this ONE helper) runs a plain
--
--   SELECT scope_id FROM role_assignment
--    WHERE user_id = :user_id AND scope_type = 'organization' ...
--
-- through api.db.get_bootstrap_db_session — deliberately the session with NO
-- app.current_org_id set, because at this point in the request there is no
-- verified tenant context yet; finding one is the whole point of the query.
--
-- role_assignment_select (0009_authorization.sql) reads:
--
--   using (scope_type = 'organization' and scope_id = app.current_org_id() ...)
--
-- and app.current_org_id() (0001_tenancy_core.sql) is
-- `current_setting('app.current_org_id', true)`, which is NULL when unset.
-- `scope_id = NULL` is never true in SQL, so the RLS predicate is false for
-- EVERY row, for EVERY caller, unconditionally — the bootstrap session
-- cannot see a row belonging to any organization, including one the caller
-- genuinely just created and was granted the owner role on at signup. This
-- is not a data bug: the row is there (SqlSignupService's founding grant
-- inserts it, and the signup response's own JWT carries the resulting
-- organization_id), and it is not a permissions bug either — it is RLS
-- correctly doing exactly what it is designed to do, asked a question that
-- cannot be answered without already knowing the answer.
--
-- The signup route never hit this because it never calls
-- _home_organization_id at all: it already has organization_id from
-- SignupService.signup()'s own return value. Every OTHER auth path that
-- needs to answer "which organization does this already-authenticated user
-- belong to" does go through this helper, so this was a complete,
-- unconditional break of returning-user login, invisible until now because
-- (a) no migration in this repository had ever been run against a real
-- Postgres before today, and (b) the existing test coverage for these routes
-- drives fake repositories that never modelled RLS at all — see
-- tests/support/fake_auth_repository.py and this migration's companion test.
--
-- --- The fix ---
--
-- The same pattern this file's own header (0001, "SECURITY DEFINER, owned by
-- ledgr_bootstrap") already establishes for the two cases that face the
-- identical problem in the opposite direction — INSERTing the very first row
-- of a tenant that RLS would otherwise never allow: a narrowly-scoped
-- SECURITY DEFINER function, owned by ledgr_bootstrap (BYPASSRLS, NOLOGIN —
-- see 0001's role comment), doing exactly the query above and nothing else.
--
-- This is now ledgr_bootstrap's THIRD function, not "exactly two" as 0001's
-- own comment still says — that comment is prose only (no migration here
-- edits 0001's DDL, which already shipped and stays immutable) and is
-- corrected below to point here, for the next reader who takes "exactly two"
-- at face value the way this bug's absence of a login test did.
--
-- Scope is deliberately the same as the two existing functions: it answers
-- one narrow question (which organization is this user's home organization)
-- for a caller that already holds a valid ledgr_app connection, and reveals
-- nothing beyond a single organization_id — not the row, not the role, not
-- any other user's assignments. api.auth.routes only ever calls it with the
-- id of the user who has JUST been authenticated by password, passkey
-- signature or a verified Google identity token in the same request — never
-- with an id taken from unauthenticated input.
-- ===========================================================================

create or replace function app.user_home_organization_id(p_user_id uuid)
returns uuid
language sql
stable
security definer
set search_path = public, app
as $$
    select scope_id
      from role_assignment
     where user_id = p_user_id
       and scope_type = 'organization'
       and revoked_at is null
       and (expires_at is null or expires_at > now())
     order by created_at asc
     limit 1;
$$;

alter function app.user_home_organization_id(uuid) owner to ledgr_bootstrap;

revoke all on function app.user_home_organization_id(uuid) from public;
grant execute on function app.user_home_organization_id(uuid) to ledgr_app;

-- 0025_bootstrap_privileges.sql already documented this exact mistake once,
-- for the original two functions: "BYPASSRLS exempts a role from row-level
-- security POLICIES; it grants no privilege on any table." SECURITY DEFINER
-- plus BYPASSRLS gets the function PAST role_assignment_select's policy, but
-- ledgr_bootstrap was never granted the ordinary table-level SELECT the
-- policy is layered on top of - so, on first real test against a live
-- Postgres, this function failed with exactly the `permission denied for
-- table role_assignment` 0025 describes, for exactly the reason 0025 names.
-- One line, following that migration's own precedent instead of repeating
-- its investigation.
grant select on role_assignment to ledgr_bootstrap;

comment on function app.user_home_organization_id(uuid) is
    'Bypasses RLS by design (SECURITY DEFINER, owned by ledgr_bootstrap) - '
    'the login path''s only way to answer "which organization does this '
    'user belong to" before any tenant context can be set. See this '
    'migration''s header for the bug this closes. Mirrors '
    'app.signup_self_managed_organization / app.create_firm_client_administration '
    '(0001_tenancy_core.sql), ledgr_bootstrap''s other two functions.';

commit;
