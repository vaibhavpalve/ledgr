-- 0026_administration_self_visibility.sql
-- Fixes an RLS policy in 0001 that makes `INSERT INTO administration ...
-- RETURNING` impossible for ledgr_app - which is every path that creates an
-- administration and needs its id back.
--
-- Touches IAM-001..005 (tenant isolation) and FR-ONB-004/005, whose onboarding
-- flow creates an administration and immediately seeds its chart of accounts
-- against the id it did not get back.
--
-- ===========================================================================
-- A policy that cannot see the row it is about to return
-- ===========================================================================
--
-- 0001's SELECT policy on `administration` reads:
--
--     using (app.has_administration_access(id))
--
-- which resolves through app.is_own_administration(id):
--
--     select exists (
--         select 1 from administration a
--         where a.id = target_administration_id
--           and a.organization_id = app.current_org_id()
--     );
--
-- On every OTHER table that helper is exactly right: `fiscal_year`, `period`,
-- `journal_line` and the rest pass their own administration_id, and the
-- administration they name is an existing, committed row. On `administration`
-- itself it is a self-lookup, and that is where it breaks.
--
-- Postgres applies the SELECT policy to the row produced by INSERT ...
-- RETURNING. The helper is STABLE, so it runs against the snapshot taken when
-- the statement began - which is before the row existed. The lookup finds
-- nothing, the policy denies, and the insert fails with
--
--     new row violates row-level security policy for table "administration"
--
-- while `app.current_org_id()` demonstrably returns the right organization and
-- the same INSERT without RETURNING succeeds. That gap between the two is what
-- made this hard to see: the policy is not wrong about the tenant, it is wrong
-- about WHEN it can look.
--
-- ===========================================================================
-- The fix, and why it takes nothing away
-- ===========================================================================
--
-- `administration` carries organization_id as a column. For this table,
--
--     app.is_own_administration(id)   ==   organization_id = app.current_org_id()
--
-- for every committed row - the helper's whole body is that comparison, done
-- the long way round through a subquery. Reading the column directly is the
-- same predicate, and it needs no snapshot, so it holds for a row that does
-- not exist yet.
--
-- The firm-engagement half is left exactly as it was, so a firm keeps seeing
-- its clients' administrations and nothing else changes. This is not a
-- widening: a row satisfying the new disjunct satisfied the old one too, once
-- it was visible.
--
-- It is also cheaper. Every SELECT against this table was doing a subquery
-- into the same table to learn what the row in hand already said.
--
-- ===========================================================================
-- Additive and reversible (NFR-044)
-- ===========================================================================
--
-- Two policies are replaced in place. Reversing it is restoring 0001's
-- definitions, which returns the system to the state where an administration
-- cannot be created through the application - worth saying plainly, because
-- reversible does not mean safe to reverse.

begin;

-- ---------------------------------------------------------------------------
-- SELECT
-- ---------------------------------------------------------------------------
drop policy administration_select on administration;

create policy administration_select on administration
    for select
    using (
        -- The row's own tenant column: true without consulting a snapshot,
        -- which is what makes INSERT ... RETURNING work.
        organization_id = app.current_org_id()
        -- Ownership OR active engagement, unchanged. Still routed through the
        -- shared helper so the firm-side rule has one definition.
        or app.has_administration_access(id)
    );

-- ---------------------------------------------------------------------------
-- UPDATE
-- ---------------------------------------------------------------------------
-- The same latent bug, one step further out. USING is evaluated against the
-- OLD row, which is committed and visible, so today's updates work. WITH CHECK
-- is evaluated against the NEW row - and an administration created and then
-- updated inside a single transaction is invisible to the STABLE lookup for
-- exactly the same reason as above. Onboarding does precisely that.
drop policy administration_update on administration;

create policy administration_update on administration
    for update
    using (
        organization_id = app.current_org_id()
        or app.has_administration_access(id)
    )
    with check (
        organization_id = app.current_org_id()
        or app.has_administration_access(id)
    );

-- No change to administration_insert_own: it already compares the column
-- directly - `with check (organization_id = app.current_org_id())` - which is
-- why the INSERT itself was never the failing half.

commit;
