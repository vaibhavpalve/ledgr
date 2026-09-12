-- 0011_custom_role_composition.sql
-- IAM-036: "Custom roles can be composed by organization admins, but only
-- from permissions they themselves hold - no privilege escalation by role
-- authoring." See docs/decisions/ADR-013-custom-role-composition.md.
--
-- A custom role may be composed from raw permissions, from other roles, or
-- both. role_component records the roles it was built from.
--
-- --- Composition is STATIC, and that is the whole security argument ---
--
-- At composition time the effective permission set is flattened into
-- role_permission, exactly as if it had been listed by hand. role_component
-- is provenance - it records what the author built from, for access reviews
-- (IAM-040+) and for a UI that wants to show it - and is NEVER consulted
-- when a request is authorized.
--
-- The alternative, resolving components at evaluation time, would mean a
-- component role gaining a permission later silently widens every role that
-- nests it, and therefore widens what the author was authorized to hand out,
-- with no new check and nobody accountable. Flattening pins a composed role
-- to the permissions its author actually held at the moment they were
-- checked against.
--
-- Two consequences follow, both good:
--   - Evaluation stays a flat join. No recursive CTE ever runs on the
--     request path (api.authz.repository's _LIVE_GRANTS_SQL is unchanged).
--   - Nesting is transitive for free. Every role's role_permission rows are
--     already complete, so composing from a role that was itself composed
--     picks up its full closure in one flat union - api.authz.service never
--     recurses either.
--
-- --- Cycles ---
--
-- Structurally impossible already: role_immutable_trg (0009) fixes a role's
-- identity at creation and there is no UPDATE or DELETE path for
-- role_component, so an edge can only ever point at a role that already
-- existed when the composing role was created. The guard below is a second
-- line, because "impossible" here rests on two separate invariants holding
-- at once, and a cycle would mean an unbounded walk in any future consumer
-- of this table.

begin;

create table role_component (
    role_id             uuid not null references "role"(id),
    component_role_id   uuid not null references "role"(id),
    created_at          timestamptz not null default now(),
    primary key (role_id, component_role_id)
);

-- "Which composed roles include this one?" - the question an access review
-- asks when a role's contents are questioned.
create index role_component_component_idx on role_component(component_role_id);

create or replace function role_component_guard() returns trigger as $$
declare
    v_role "role";
    v_component "role";
begin
    if new.role_id = new.component_role_id then
        raise exception 'a role cannot include itself';
    end if;

    select * into v_role from "role" where id = new.role_id;
    select * into v_component from "role" where id = new.component_role_id;

    if v_role.id is null then
        raise exception 'composing role % does not exist or is not visible', new.role_id;
    end if;
    if v_component.id is null then
        raise exception 'component role % does not exist or is not visible', new.component_role_id;
    end if;

    -- A system role's composition is fixed by migration. Only custom roles
    -- are composed, and only out of roles their organization can see.
    if v_role.is_system then
        raise exception 'a system role cannot be composed from other roles';
    end if;
    if not v_component.is_system and v_component.organization_id is distinct from
        v_role.organization_id then
        raise exception 'component role % belongs to another organization', new.component_role_id;
    end if;
    if v_component.archived_at is not null then
        raise exception 'component role % is archived', new.component_role_id;
    end if;

    -- An administration-scope role including an organization-scope role would
    -- pull organization-level permissions into an administration-level grant.
    -- role_permission_scope_guard_trg (0009) would reject the resulting rows
    -- anyway; failing here names the actual mistake instead.
    if v_role.scope_type = 'administration' and v_component.scope_type = 'organization' then
        raise exception
            'an administration-scope role cannot include the organization-scope role % '
            '(IAM-032)', v_component.name;
    end if;

    -- Defensive; see the header. Note this runs with the invoker's
    -- privileges, so RLS applies to the walk - acceptable precisely because
    -- it is a second line behind immutability, not the primary guarantee.
    if exists (
        with recursive reachable as (
            select new.component_role_id as id
            union
            select rc.component_role_id
            from role_component rc
            join reachable r on rc.role_id = r.id
        )
        select 1 from reachable where id = new.role_id
    ) then
        raise exception 'including role % would create a cycle', new.component_role_id;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger role_component_guard_trg
    before insert on role_component
    for each row execute function role_component_guard();

-- Composition is fixed at creation, like everything else about a custom role
-- (role_immutable_trg, 0009). Re-composing means creating a new role.
create or replace function role_component_immutable() returns trigger as $$
begin
    raise exception 'role composition is immutable - compose a replacement role instead';
end;
$$ language plpgsql;

create trigger role_component_immutable_trg
    before update on role_component
    for each row execute function role_component_immutable();

alter table role_component owner to ledgr_migrator;

-- No UPDATE and no DELETE grant: a composed role's provenance outlives the
-- role, for the same reason revoked assignments are kept (IAM-040).
grant select, insert on role_component to ledgr_app;

alter table role_component enable row level security;
alter table role_component force row level security;

create policy role_component_select on role_component
    for select
    using (
        exists (
            select 1 from "role" r
            where r.id = role_component.role_id
              and (r.is_system or r.organization_id = app.current_org_id())
        )
    );

-- Insert only into your own custom role, and only naming a component that
-- organization may itself see.
create policy role_component_insert on role_component
    for insert
    with check (
        exists (
            select 1 from "role" r
            where r.id = role_component.role_id
              and not r.is_system
              and r.organization_id = app.current_org_id()
        )
        and exists (
            select 1 from "role" c
            where c.id = role_component.component_role_id
              and (c.is_system or c.organization_id = app.current_org_id())
        )
    );

commit;
