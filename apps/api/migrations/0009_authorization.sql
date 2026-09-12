-- 0009_authorization.sql
-- IAM-030 through IAM-037: the RBAC/ABAC model behind CLAUDE.md's third
-- non-negotiable ("one authorization library, used everywhere"). See
-- docs/decisions/ADR-011-authorization-model.md.
--
-- Four tables:
--   permission        the (action, resource_type, scope) triple - IAM-030.
--   "role"            a named bundle. organization_id null = built-in
--                     system role; set = a custom role composed by that
--                     organization's admins (IAM-036).
--   role_permission   bundle contents.
--   role_assignment   who holds which bundle, where, under which attribute
--                     conditions (IAM-033), until when (IAM-035).
--
-- "role" is quoted throughout: ROLE is an unreserved keyword in PostgreSQL
-- and would parse unquoted, but GRANT/ALTER statements read ambiguously
-- next to Postgres's own database roles. Quoting removes the ambiguity for
-- a human reader as much as for the parser.
--
-- Two words spelled "scope" appear here and they are NOT the same thing:
--   permission.resource_scope  the level an action inherently operates at.
--                              "manage organization settings" is an
--                              organization-level action; "post a journal
--                              entry" is an administration-level one.
--   *.scope_type / scope_id    WHERE a particular role or grant applies:
--                              one organization, or one administration.
--                              This is the one IAM-032 governs.
--
-- IAM-032 ("scope narrowing only: a grant at administration level cannot be
-- widened to organization level by any code path") is enforced structurally
-- in three independent places, not by convention:
--   1. role_assignment_immutable_trg below makes user_id, role_id,
--      scope_type, scope_id, conditions and expires_at immutable after
--      INSERT. There is no UPDATE that can widen an existing grant, because
--      every column that defines its reach rejects modification. Widening
--      requires revoking and re-granting - a new row, newly authorized,
--      newly attributable to a granter.
--   2. role_permission_scope_guard_trg refuses to bundle an
--      organization-scope permission into an administration-scope role, so
--      an administration-scoped assignment can never carry an
--      organization-level capability in the first place.
--   3. api.authz.service.AuthorizationService only ever matches an
--      administration-scoped grant against an exact administration target
--      (see its authorize() method) - the widening direction is not
--      expressible in the evaluation code either.

begin;

-- ---------------------------------------------------------------------------
-- permission
-- ---------------------------------------------------------------------------
create table permission (
    id              uuid primary key default gen_random_uuid(),
    action          text not null,
    resource_type   text not null,
    resource_scope  text not null check (resource_scope in ('organization', 'administration')),
    description     text not null,
    constraint permission_unique_triple unique (action, resource_type, resource_scope)
);

-- ---------------------------------------------------------------------------
-- role
-- ---------------------------------------------------------------------------
create table "role" (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid references organization(id),
    name                text not null,
    scope_type          text not null check (scope_type in ('organization', 'administration')),
    is_system           boolean not null default false,
    created_by_user_id  uuid references users(id),
    archived_at         timestamptz,
    created_at          timestamptz not null default now(),
    -- A system role belongs to no organization; a custom role must.
    constraint role_system_has_no_org check (
        (is_system and organization_id is null)
        or (not is_system and organization_id is not null)
    )
);

create unique index role_system_name_unique on "role"(name) where is_system;
create unique index role_custom_name_unique on "role"(organization_id, name) where not is_system;
create index role_organization_idx on "role"(organization_id) where organization_id is not null;

-- ---------------------------------------------------------------------------
-- role_permission
-- ---------------------------------------------------------------------------
create table role_permission (
    role_id         uuid not null references "role"(id),
    permission_id   uuid not null references permission(id),
    primary key (role_id, permission_id)
);

create index role_permission_permission_idx on role_permission(permission_id);

-- ---------------------------------------------------------------------------
-- role_assignment
-- ---------------------------------------------------------------------------
create table role_assignment (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null references users(id),
    role_id             uuid not null references "role"(id),
    scope_type          text not null check (scope_type in ('organization', 'administration')),
    -- Deliberately NOT a foreign key: it references organization(id) or
    -- administration(id) depending on scope_type, and a single FK column
    -- cannot target two tables. role_assignment_scope_guard_trg below does
    -- the referential check instead - the same trigger-over-FK tradeoff
    -- firm_engagement_guard already makes in 0001 for the same reason.
    scope_id            uuid not null,
    conditions          jsonb not null default '{}',
    granted_by_user_id  uuid not null references users(id),
    expires_at          timestamptz,
    revoked_at          timestamptz,
    created_at          timestamptz not null default now()
);

-- The evaluation query's shape: one user's live grants, every request.
create index role_assignment_live_by_user_idx
    on role_assignment(user_id, scope_type, scope_id)
    where revoked_at is null;

-- Access reviews (IAM-040+) ask the inverse question: who holds anything
-- on this scope?
create index role_assignment_by_scope_idx
    on role_assignment(scope_type, scope_id)
    where revoked_at is null;

create index role_assignment_role_idx on role_assignment(role_id);

-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------

-- A role's scope_type constrains which permissions may be bundled into it.
-- An administration-scope role may hold administration-scope permissions
-- only; an organization-scope role may hold either, because an
-- organization-scoped grant legitimately reaches the administrations that
-- organization owns (Appendix A: Org Admin, an organization-scoped role,
-- holds "Submit expenses", an administration-level capability). The reverse
-- direction is the widening IAM-032 forbids.
create or replace function role_permission_scope_guard() returns trigger as $$
declare
    v_role_scope text;
    v_permission_scope text;
begin
    select scope_type into v_role_scope from "role" where id = new.role_id;
    select resource_scope into v_permission_scope from permission where id = new.permission_id;

    if v_role_scope = 'administration' and v_permission_scope = 'organization' then
        raise exception
            'cannot bundle an organization-scope permission into an administration-scope role '
            '(IAM-032: an administration-level grant must not carry organization-level reach)';
    end if;

    return new;
end;
$$ language plpgsql;

create trigger role_permission_scope_guard_trg
    before insert on role_permission
    for each row execute function role_permission_scope_guard();

-- Referential integrity for the polymorphic scope_id, plus the invariant
-- that an assignment's scope_type always matches its role's.
create or replace function role_assignment_scope_guard() returns trigger as $$
declare
    v_role_scope text;
    v_role_org uuid;
    v_role_is_system boolean;
    v_role_archived timestamptz;
begin
    select scope_type, organization_id, is_system, archived_at
    into v_role_scope, v_role_org, v_role_is_system, v_role_archived
    from "role" where id = new.role_id;

    if v_role_scope is distinct from new.scope_type then
        raise exception
            'role_assignment.scope_type (%) must match the role''s scope_type (%)',
            new.scope_type, v_role_scope;
    end if;

    if v_role_archived is not null then
        raise exception 'role % is archived and cannot be newly assigned', new.role_id;
    end if;

    if new.scope_type = 'organization' then
        if not exists (select 1 from organization where id = new.scope_id) then
            raise exception 'role_assignment.scope_id % is not a visible organization', new.scope_id;
        end if;
        -- A custom role is assignable only within the organization that
        -- composed it. Without this, org A could hand org B a bundle org B's
        -- own admins never reviewed.
        if not v_role_is_system and v_role_org is distinct from new.scope_id then
            raise exception 'custom role % belongs to organization %, not %',
                new.role_id, v_role_org, new.scope_id;
        end if;
    else
        if not exists (select 1 from administration where id = new.scope_id) then
            raise exception 'role_assignment.scope_id % is not a visible administration',
                new.scope_id;
        end if;
        if not v_role_is_system and v_role_org is distinct from (
            select organization_id from administration where id = new.scope_id
        ) then
            raise exception 'custom role % may only be assigned within organization %',
                new.role_id, v_role_org;
        end if;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger role_assignment_scope_guard_trg
    before insert on role_assignment
    for each row execute function role_assignment_scope_guard();

-- IAM-033's condition set is closed. An unrecognized key stored here would
-- be silently ignored by a naive evaluator, turning a typo into a granted
-- permission; api.authz.conditions denies on an unknown key for that
-- reason, and this trigger stops one being written at all.
--
-- amount_ceiling is stored as a decimal STRING, never a JSON number:
-- jsonb's number type is IEEE 754 double precision, and NFR-031 forbids
-- floating point anywhere in the calculation path. An amount ceiling is
-- squarely in that path - it is compared against transaction amounts.
create or replace function role_assignment_conditions_guard() returns trigger as $$
declare
    v_key text;
begin
    for v_key in select jsonb_object_keys(new.conditions)
    loop
        if v_key not in (
            'amount_ceiling', 'cost_centre_ids', 'journal_ids', 'period_ids', 'ip_allowlist'
        ) then
            raise exception
                'unknown attribute condition %; IAM-033''s condition set is closed '
                '(amount_ceiling, cost_centre_ids, journal_ids, period_ids, ip_allowlist)',
                v_key;
        end if;
    end loop;

    if new.conditions ? 'amount_ceiling' then
        if jsonb_typeof(new.conditions -> 'amount_ceiling') <> 'string' then
            raise exception
                'amount_ceiling must be a decimal string, not a JSON number '
                '(NFR-031: no financial calculation uses floating point)';
        end if;
        -- Parse it here so a malformed ceiling fails at grant time, loudly,
        -- rather than at evaluation time on some later request.
        perform (new.conditions ->> 'amount_ceiling')::numeric;
    end if;

    foreach v_key in array array['cost_centre_ids', 'journal_ids', 'period_ids', 'ip_allowlist']
    loop
        if new.conditions ? v_key and jsonb_typeof(new.conditions -> v_key) <> 'array' then
            raise exception 'attribute condition % must be a JSON array', v_key;
        end if;
    end loop;

    return new;
end;
$$ language plpgsql;

create trigger role_assignment_conditions_guard_trg
    before insert on role_assignment
    for each row execute function role_assignment_conditions_guard();

-- The IAM-032 backstop. Everything that defines a grant's reach is
-- immutable; revoked_at is the only column an UPDATE may change, and only
-- in the tightening direction. Un-revoking is not an edit, it is a new
-- grant, and must be authorized and attributed as one.
create or replace function role_assignment_immutable() returns trigger as $$
begin
    if new.user_id is distinct from old.user_id
        or new.role_id is distinct from old.role_id
        or new.scope_type is distinct from old.scope_type
        or new.scope_id is distinct from old.scope_id
        or new.conditions is distinct from old.conditions
        or new.expires_at is distinct from old.expires_at
        or new.granted_by_user_id is distinct from old.granted_by_user_id
        or new.created_at is distinct from old.created_at
    then
        raise exception
            'a role assignment''s user, role, scope, conditions and expiry are immutable '
            '(IAM-032) - revoke this assignment and create a new one instead';
    end if;

    if old.revoked_at is not null and new.revoked_at is distinct from old.revoked_at then
        raise exception 'a revoked role assignment cannot be un-revoked or re-dated';
    end if;

    return new;
end;
$$ language plpgsql;

create trigger role_assignment_immutable_trg
    before update on role_assignment
    for each row execute function role_assignment_immutable();

-- Custom roles are immutable once composed, for the same reason grants are:
-- editing a role that is already assigned to many users silently re-grants
-- to all of them, with no new authorization check and nothing attributable.
-- Retiring a custom role sets archived_at; replacing one means composing a
-- new role and re-assigning.
create or replace function role_immutable() returns trigger as $$
begin
    if new.organization_id is distinct from old.organization_id
        or new.name is distinct from old.name
        or new.scope_type is distinct from old.scope_type
        or new.is_system is distinct from old.is_system
        or new.created_by_user_id is distinct from old.created_by_user_id
    then
        raise exception
            'a role''s identity and scope are immutable (IAM-036) - archive it and '
            'compose a replacement instead';
    end if;
    return new;
end;
$$ language plpgsql;

create trigger role_immutable_trg
    before update on "role"
    for each row execute function role_immutable();

-- ---------------------------------------------------------------------------
-- Ownership and privilege grants
-- ---------------------------------------------------------------------------
alter table permission       owner to ledgr_migrator;
alter table "role"           owner to ledgr_migrator;
alter table role_permission  owner to ledgr_migrator;
alter table role_assignment  owner to ledgr_migrator;

-- permission is reference data defined by migrations. ledgr_app reads it
-- and never writes it: a running application cannot invent a new capability
-- for itself.
grant select on permission to ledgr_app;

grant select, insert, update on "role" to ledgr_app;
grant select, insert on role_permission to ledgr_app;
grant select, insert, update on role_assignment to ledgr_app;

-- No DELETE grant anywhere, consistent with the rest of this schema. Roles
-- are archived, assignments are revoked; IAM-040's access reviews need the
-- history of who held what and when, which a deleted row cannot answer.

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------
alter table permission enable row level security;
alter table permission force row level security;

alter table "role" enable row level security;
alter table "role" force row level security;

alter table role_permission enable row level security;
alter table role_permission force row level security;

alter table role_assignment enable row level security;
alter table role_assignment force row level security;

-- --- permission -------------------------------------------------------------
-- Global reference data with no tenant dimension: the catalogue of what
-- actions exist is identical for every tenant and reveals nothing about
-- any tenant's data. RLS is still enabled with an explicit using(true) so
-- that "this table has no tenant predicate" is a visible, reviewed decision
-- in the policy list rather than a table someone forgot to protect.
create policy permission_select on permission for select using (true);

-- --- role -------------------------------------------------------------------
create policy role_select on "role"
    for select
    using (is_system or organization_id = app.current_org_id());

create policy role_insert on "role"
    for insert
    with check (not is_system and organization_id = app.current_org_id());

-- Archiving only; role_immutable_trg rejects every other field change.
create policy role_update on "role"
    for update
    using (not is_system and organization_id = app.current_org_id())
    with check (not is_system and organization_id = app.current_org_id());

-- --- role_permission --------------------------------------------------------
create policy role_permission_select on role_permission
    for select
    using (
        exists (
            select 1 from "role" r
            where r.id = role_permission.role_id
              and (r.is_system or r.organization_id = app.current_org_id())
        )
    );

-- Only into your own custom roles. A system role's contents are fixed by
-- migration and cannot be extended at runtime by anyone.
create policy role_permission_insert on role_permission
    for insert
    with check (
        exists (
            select 1 from "role" r
            where r.id = role_permission.role_id
              and not r.is_system
              and r.organization_id = app.current_org_id()
        )
    );

-- --- role_assignment --------------------------------------------------------
-- The tenant predicate follows the scope, not a denormalized column: an
-- organization-scoped assignment is visible to that organization; an
-- administration-scoped one to whoever may reach that administration at all
-- (its owner, or a firm with an active engagement - app.has_administration_access
-- from 0001). This is what lets a firm see and manage the assignments its
-- staff hold on a client administration without ever seeing the client's
-- organization-level grants.
create policy role_assignment_select on role_assignment
    for select
    using (
        (scope_type = 'organization' and scope_id = app.current_org_id())
        or (scope_type = 'administration' and app.has_administration_access(scope_id))
    );

create policy role_assignment_insert on role_assignment
    for insert
    with check (
        (scope_type = 'organization' and scope_id = app.current_org_id())
        or (scope_type = 'administration' and app.has_administration_access(scope_id))
    );

-- Revocation only - role_assignment_immutable_trg rejects everything else.
create policy role_assignment_update on role_assignment
    for update
    using (
        (scope_type = 'organization' and scope_id = app.current_org_id())
        or (scope_type = 'administration' and app.has_administration_access(scope_id))
    )
    with check (
        (scope_type = 'organization' and scope_id = app.current_org_id())
        or (scope_type = 'administration' and app.has_administration_access(scope_id))
    );

-- The permission catalogue and the twelve standard roles are NOT seeded here.
-- They are data derived from PRD Appendix A and §8.4, generated into
-- 0010_role_catalogue.sql from apps/api/src/api/authz/matrix.py. Keeping the
-- structure (this file, hand-designed) separate from the catalogue (that
-- file, generated) is what lets a test regenerate and compare without
-- diffing around hand-written DDL.

commit;