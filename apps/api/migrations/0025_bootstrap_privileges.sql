-- 0025_bootstrap_privileges.sql
-- Fixes a privilege gap in 0001, 0002 and 0022 that makes signup, firm client
-- provisioning and the idempotency purge fail at runtime.
--
-- No requirement introduces this; it repairs the ones already implemented:
--   FR-ONB-001  self-service signup      app.signup_self_managed_organization
--   FR-MDL-004  firm creates a client    app.create_firm_client_administration
--   NFR-032     idempotency key expiry   app.purge_expired_idempotency_keys
--
-- ===========================================================================
-- BYPASSRLS is not a table privilege
-- ===========================================================================
--
-- All three functions are SECURITY DEFINER and owned by ledgr_bootstrap, which
-- 0001 describes precisely:
--
--   "Owned by ledgr_bootstrap (BYPASSRLS, NOLOGIN). ledgr_app is granted
--    EXECUTE and nothing more - it can call these two functions and cannot
--    reach BYPASSRLS any other way, because no other object in the database is
--    owned by, or grants privileges equivalent to, ledgr_bootstrap."
--
-- That design is right and this migration does not change it. What was missed
-- is that BYPASSRLS and a table privilege are different things. BYPASSRLS
-- exempts a role from row-level security POLICIES; it grants no privilege on
-- any table. ledgr_bootstrap owned three definer functions and held SELECT,
-- INSERT and DELETE on nothing at all, so every one of them failed with
-- `permission denied for table organization` the first time it was called.
--
-- The two facts hide each other. Reading 0001 you check that the policies
-- cannot block the insert - they cannot, that is what BYPASSRLS is for - and
-- the grant that was never written is not visible in the file that needed it.
-- It surfaces only when the function runs, which is why it survived until the
-- migrations were executed against a real database for the first time.
--
-- ===========================================================================
-- Exactly what each function touches, and nothing more
-- ===========================================================================
--
--   signup_self_managed_organization
--       insert into organization ... returning *      organization: INSERT, SELECT
--
--   create_firm_client_administration
--       select kind from organization                 organization: SELECT
--       insert into organization ... returning *      organization: INSERT
--       insert into administration ... returning *    administration: INSERT, SELECT
--       insert into firm_engagement                   firm_engagement: INSERT
--       insert into administration_encryption_key     ...key: INSERT
--
--   purge_expired_idempotency_keys
--       delete from idempotency_key where expires_at  idempotency_key: DELETE, SELECT
--
-- RETURNING is why SELECT appears beside INSERT: it reads back the row it just
-- wrote, and Postgres checks that as a read.
--
-- No UPDATE and no DELETE beyond the purge, which keeps 0001's stance intact:
-- "No DELETE grant anywhere: these rows are archived/revoked/closed via status
-- columns, never removed." The one DELETE here is on idempotency_key, whose
-- rows are cache entries with an expiry rather than records of anything.
--
-- ===========================================================================
-- Why a new migration rather than an edit to 0001
-- ===========================================================================
--
-- The convention this repository already follows: 0002 replaced 0001's
-- create_firm_client_administration by dropping and recreating it here rather
-- than editing 0001, and 0021 replaced 0020's journal_entry_validate() the
-- same way. NFR-044 asks for migrations that are backward compatible and
-- reversible, which an edit to an already-applied file is not.
--
-- Additive and reversible: this migration only issues GRANTs. Reversing it is
-- the matching REVOKEs, and doing so returns the system to the state where
-- signup fails - which is worth stating plainly, because "reversible" here
-- does not mean "safe to reverse".

begin;

-- FR-ONB-001 and FR-MDL-004: the tenancy tables the two bootstrap functions
-- create rows in before any tenant context exists.
grant select, insert on organization   to ledgr_bootstrap;
grant select, insert on administration to ledgr_bootstrap;
grant insert on firm_engagement        to ledgr_bootstrap;

-- SEC-022: the first envelope key, written atomically with the administration
-- it protects (0002). INSERT only - the function never reads a key back, and a
-- bootstrap role that could SELECT wrapped DEKs would widen the blast radius
-- of the one role in the system holding BYPASSRLS.
grant insert on administration_encryption_key to ledgr_bootstrap;

-- NFR-032: expiry. SELECT because the WHERE clause reads expires_at.
grant select, delete on idempotency_key to ledgr_bootstrap;

-- FR-MDL-004 again, a third instance of the same shape of gap: inserting
-- into administration fires administration_assign_colour_trg (0018), which
-- is plain plpgsql with no SECURITY DEFINER of its own - so for the
-- duration of create_firm_client_administration's call it runs as this
-- function's owner too, and needs its own SELECT on client_colour rather
-- than borrowing ledgr_app's (0018 grants that to ledgr_app alone; every
-- direct application insert into administration goes through ledgr_app, so
-- nothing before this exercised the bootstrap path's own trigger firing).
grant select on client_colour to ledgr_bootstrap;

-- FR-MDL-004: the same gap this migration's own docstring describes, found
-- the same way (a real Postgres, not a read of the migration) - the one
-- table/schema kind neither the table-privilege audit above nor a code
-- review would think to check. create_firm_client_administration calls
-- app.current_org_id() from inside its own SECURITY DEFINER body (it needs
-- the calling firm's org id to check `kind = 'firm'`) - unlike
-- signup_self_managed_organization/signup_firm_organization, which run
-- before any tenant context exists and never reach into `app` at all, and
-- unlike purge_expired_idempotency_keys, whose DELETE never does either.
-- Owning objects IN a schema (ledgr_bootstrap owns all three bootstrap
-- functions) does not imply USAGE ON that schema - two separate grants -
-- and nothing before this migration ever gave ledgr_bootstrap the second
-- one. The result: every OTHER call in create_firm_client_administration's
-- body would have worked, and only the one line calling app.current_org_id()
-- failed, with "permission denied for schema app" - which does not name the
-- function, the table, or even hint that ownership already covers
-- everything else it touches.
grant usage on schema app to ledgr_bootstrap;

-- NFR-032, the same gap: app.purge_expired_idempotency_keys() is granted
-- EXECUTE to ledgr_ops (0022) but calling a schema-qualified function also
-- needs USAGE on the schema it lives in, which nothing ever granted ledgr_ops
-- for `app` (only for `ledger`, in 0020) - "permission denied for schema
-- app" the first time the purge job actually ran as ledgr_ops rather than
-- being read off the grant list.
grant usage on schema app to ledgr_ops;

-- ---------------------------------------------------------------------------
-- The check that would have caught this
-- ---------------------------------------------------------------------------
-- A SECURITY DEFINER function is only as capable as its owner, and its owner's
-- privileges live in a different file from the function. This function makes
-- the pairing inspectable: it lists every definer function in `app` and
-- `ledger` beside the role it runs as, so a reviewer can ask "and does that
-- role hold what the body needs" without reading five migrations.
--
-- tests/integration/test_bootstrap_privileges.py asserts the three functions
-- above can actually be executed, which is the part a privilege listing cannot
-- tell you.
create or replace function app.security_definer_surface()
returns table (
    schema_name     text,
    function_name   text,
    runs_as         text,
    owner_can_login boolean,
    owner_bypasses_rls boolean
)
language sql
stable
as $$
    select n.nspname::text,
           p.proname::text,
           pg_get_userbyid(p.proowner)::text,
           r.rolcanlogin,
           r.rolbypassrls
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
      join pg_roles r on r.oid = p.proowner
     where p.prosecdef
       and n.nspname in ('app', 'ledger')
     order by n.nspname, p.proname;
$$;

comment on function app.security_definer_surface() is
    'Every SECURITY DEFINER function and the role it runs as. The privilege '
    'escalation surface of this database, in one query - see 0025.';

grant execute on function app.security_definer_surface() to ledgr_app, ledgr_ops;

commit;
