-- 0001_tenancy_core.sql
-- Tenancy core: Organization, Administration, the firm/client access grant,
-- FiscalYear, Period — plus the row-level security that enforces IAM-002
-- ("tenant isolation is enforced at the data layer, not only in application
-- code") at the database itself. See docs/decisions/ADR-002-tenancy-schema.md
-- for the table shape and docs/decisions/ADR-003-rls-enforcement.md for the
-- enforcement mechanism (roles, session variable, bootstrap functions).
--
-- Both account models (firm-led and self-managed) run through these same
-- tables. There is no separate "firm path" — a self-managed business is an
-- organization that owns its own administration directly; a firm is an
-- organization that is granted access to a client's administration via
-- firm_engagement. The tables don't know or care which model produced a
-- given row.
--
-- This migration must be applied by a role with permission to CREATE ROLE,
-- CREATE SCHEMA and CREATE EXTENSION (typically a one-off admin/bootstrap
-- connection, not any role used day to day). Everything created here ends
-- up owned by ledgr_migrator; the live API never authenticates as the role
-- that ran this file.

begin;

create extension if not exists "pgcrypto"; -- gen_random_uuid()

create schema if not exists app;

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------
-- Three roles, three jobs. This separation is what makes RLS bypass require
-- a deliberate, reviewed act rather than an accident:
--
--   ledgr_migrator  - owns every table. Runs migrations (CI/ops only).
--                     NOSUPERUSER, NOBYPASSRLS. FORCE ROW LEVEL SECURITY
--                     (below) means even this owning role is subject to
--                     RLS at query time — owning a table is not, by itself,
--                     a bypass.
--   ledgr_app       - what the API authenticates as for every request.
--                     NOSUPERUSER, NOBYPASSRLS, no table ownership. Granted
--                     SELECT/INSERT/UPDATE only — DELETE is never granted
--                     at the privilege level, so no policy bug could ever
--                     make a delete succeed.
--   ledgr_bootstrap - NOLOGIN. Cannot be connected to directly, by anyone,
--                     ever. Owns the two SECURITY DEFINER functions below
--                     plus a third added later (login's home-organization
--                     lookup — see 0048_login_home_organization_lookup.sql
--                     for why a read needed this too, not only the writes
--                     below) and is the only role in the system with
--                     BYPASSRLS. That makes the bypass surface exactly the
--                     bodies of those functions — not a role, not a
--                     connection string, not a config flag anyone could
--                     flip by accident.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'ledgr_migrator') then
        create role ledgr_migrator nosuperuser nocreatedb nocreaterole nobypassrls login password null;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'ledgr_app') then
        create role ledgr_app nosuperuser nocreatedb nocreaterole nobypassrls login password null;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'ledgr_bootstrap') then
        create role ledgr_bootstrap nosuperuser nocreatedb nocreaterole bypassrls nologin;
    end if;
end;
$$;

-- Real deployments provision these roles (and their passwords/auth) via
-- infra-as-code per SEC-030, not with literal `password null`. The DO block
-- above exists so this migration is self-contained and idempotent in a
-- fresh local/CI database.

-- ---------------------------------------------------------------------------
-- organization
-- ---------------------------------------------------------------------------
create table organization (
    id                          uuid primary key default gen_random_uuid(),
    kind                        text not null check (kind in ('firm', 'business')),
    name                        text not null,
    kvk_number                  text,
    claimed_at                  timestamptz,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- administration
-- ---------------------------------------------------------------------------
create table administration (
    id                          uuid primary key default gen_random_uuid(),
    organization_id             uuid not null references organization(id),
    legal_name                  text not null,
    legal_form                  text not null,
    kvk_number                  text,
    vat_number                  text,
    ob_number                   text,
    fiscal_year_start_month     smallint not null default 1
                                    check (fiscal_year_start_month between 1 and 12),
    status                      text not null default 'active'
                                    check (status in ('active', 'archived')),
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

create index administration_organization_id_idx on administration(organization_id);

-- ---------------------------------------------------------------------------
-- firm_engagement
-- ---------------------------------------------------------------------------
create table firm_engagement (
    id                          uuid primary key default gen_random_uuid(),
    firm_organization_id        uuid not null references organization(id),
    administration_id           uuid not null references administration(id),
    status                      text not null default 'pending'
                                    check (status in ('pending', 'active', 'declined', 'revoked')),
    initiated_by                text not null check (initiated_by in ('firm', 'client')),
    invited_by_user_id          uuid,          -- FK to users table, added when users ships
    accepted_by_user_id         uuid,
    accepted_at                 timestamptz,
    revoked_by_user_id          uuid,
    revoked_at                  timestamptz,
    created_at                  timestamptz not null default now()
);

create index firm_engagement_firm_org_idx on firm_engagement(firm_organization_id);
create index firm_engagement_administration_idx on firm_engagement(administration_id);

create unique index firm_engagement_one_active_idx
    on firm_engagement(firm_organization_id, administration_id)
    where status = 'active';

-- ---------------------------------------------------------------------------
-- fiscal_year / period
-- ---------------------------------------------------------------------------
create table fiscal_year (
    id                          uuid primary key default gen_random_uuid(),
    organization_id             uuid not null references organization(id),
    administration_id           uuid not null references administration(id),
    start_date                  date not null,
    end_date                    date not null,
    status                      text not null default 'open'
                                    check (status in ('open', 'closed')),
    created_at                  timestamptz not null default now(),
    constraint fiscal_year_date_order check (end_date > start_date),
    constraint fiscal_year_unique_start unique (administration_id, start_date)
);

create index fiscal_year_administration_idx on fiscal_year(administration_id);
create index fiscal_year_organization_idx on fiscal_year(organization_id);

create table period (
    id                          uuid primary key default gen_random_uuid(),
    organization_id             uuid not null references organization(id),
    administration_id           uuid not null references administration(id),
    fiscal_year_id              uuid not null references fiscal_year(id),
    period_number               smallint not null,
    start_date                  date not null,
    end_date                    date not null,
    status                      text not null default 'open'
                                    check (status in ('open', 'locked', 'vat_filed')),
    locked_at                   timestamptz,
    locked_by_user_id           uuid,
    created_at                  timestamptz not null default now(),
    constraint period_date_order check (end_date > start_date),
    constraint period_unique_number unique (fiscal_year_id, period_number)
);

create index period_administration_idx on period(administration_id);
create index period_fiscal_year_idx on period(fiscal_year_id);
create index period_organization_idx on period(organization_id);

-- ---------------------------------------------------------------------------
-- Tenant context and access predicates (app schema)
-- ---------------------------------------------------------------------------
-- app.current_org_id() is the single choke point every policy below reads
-- through. It wraps current_setting with missing_ok = true, so an unset
-- variable yields SQL NULL rather than an error. NULL compared against any
-- uuid column is never TRUE — not TRUE, not FALSE, but NULL — so every
-- policy predicate below evaluates to "not satisfied" when tenant context
-- is absent. That is the fail-closed behavior: no session variable means
-- zero visible rows on every table, in every statement, with no separate
-- "unrestricted" code path to fall back to.
create or replace function app.current_org_id() returns uuid
language sql
stable
as $$
    select nullif(current_setting('app.current_org_id', true), '')::uuid;
$$;

-- Ownership-only check: is the calling session's org the administration's
-- owner? Used where engagement access must NOT count — e.g. a firm should
-- never see another firm's engagement row on the same client.
create or replace function app.is_own_administration(target_administration_id uuid)
returns boolean
language sql
stable
as $$
    select exists (
        select 1 from administration a
        where a.id = target_administration_id
          and a.organization_id = app.current_org_id()
    );
$$;

-- Ownership OR active engagement: the general "can this session touch this
-- administration at all" predicate used by administration/fiscal_year/period.
create or replace function app.has_administration_access(target_administration_id uuid)
returns boolean
language sql
stable
as $$
    select app.is_own_administration(target_administration_id)
    or exists (
        select 1 from firm_engagement fe
        where fe.administration_id = target_administration_id
          and fe.status = 'active'
          and fe.firm_organization_id = app.current_org_id()
    );
$$;

-- ---------------------------------------------------------------------------
-- Ownership and privilege grants
-- ---------------------------------------------------------------------------
alter table organization    owner to ledgr_migrator;
alter table administration  owner to ledgr_migrator;
alter table firm_engagement owner to ledgr_migrator;
alter table fiscal_year     owner to ledgr_migrator;
alter table period          owner to ledgr_migrator;

grant usage on schema app to ledgr_app;

-- No DELETE grant anywhere: these rows are archived/revoked/closed via
-- status columns, never removed. Even a bug in a future policy could not
-- make a DELETE succeed, because the privilege to attempt one does not
-- exist for ledgr_app.
grant select, insert, update on organization    to ledgr_app;
grant select, insert, update on administration  to ledgr_app;
grant select, insert, update on firm_engagement to ledgr_app;
grant select, insert, update on fiscal_year     to ledgr_app;
grant select, insert, update on period          to ledgr_app;

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------
-- FORCE ROW LEVEL SECURITY matters specifically because ledgr_migrator owns
-- these tables: without FORCE, Postgres exempts table owners from RLS by
-- default. With it, ledgr_migrator is bound by the same policies as
-- ledgr_app — table ownership stops being a bypass path. The only role in
-- the system exempt from RLS is ledgr_bootstrap (BYPASSRLS), and it has no
-- LOGIN privilege, so nothing can open a session as it.
alter table organization enable row level security;
alter table organization force row level security;

alter table administration enable row level security;
alter table administration force row level security;

alter table firm_engagement enable row level security;
alter table firm_engagement force row level security;

alter table fiscal_year enable row level security;
alter table fiscal_year force row level security;

alter table period enable row level security;
alter table period force row level security;

-- --- organization ---------------------------------------------------------

-- SELECT: your own organization, or the owning organization of any
-- administration you (a firm) currently have active engagement on.
create policy organization_select on organization
    for select
    using (
        id = app.current_org_id()
        or id in (
            select a.organization_id from administration a
            join firm_engagement fe on fe.administration_id = a.id
            where fe.status = 'active'
              and fe.firm_organization_id = app.current_org_id()
        )
    );

-- INSERT: deliberately no policy. A new organization row can only ever be
-- created by the two SECURITY DEFINER bootstrap functions below, which run
-- as ledgr_bootstrap and are therefore not subject to this table's RLS at
-- all. ledgr_app, connecting directly, cannot INSERT into organization
-- under any circumstance — there is no first-policy-that-matches path for
-- it to take.

-- UPDATE: only your own organization row (renaming, claiming). A firm's
-- engagement never grants it write access to the client's own org record —
-- engagement is about administrations, not about the client's identity.
create policy organization_update_self on organization
    for update
    using (id = app.current_org_id())
    with check (id = app.current_org_id());

-- DELETE: no policy, no grant. Organizations are deactivated, not deleted.

-- --- administration ---------------------------------------------------------

create policy administration_select on administration
    for select
    using (app.has_administration_access(id));

-- INSERT: an organization may only create administrations it owns itself.
-- This is intentionally NOT has_administration_access(id) — that predicate
-- looks the row up in this very table, which does not exist yet during an
-- INSERT and would make every insert fail. Self-managed signup (FR-ONB) is
-- the only direct-insert path; the firm-creates-an-unclaimed-client path
-- (FR-MDL-004) goes through app.create_firm_client_administration below,
-- which creates the owning organization row in the same transaction before
-- this check ever runs, so organization_id = current_org_id() still holds
-- from the bootstrap function's point of view... except the acting session
-- there is the FIRM, not the new client — which is exactly why that case is
-- also routed through a SECURITY DEFINER function rather than this policy.
create policy administration_insert_own on administration
    for insert
    with check (organization_id = app.current_org_id());

-- UPDATE: ownership or active engagement gets you past the tenant boundary.
-- This is the coarse boundary only — whether a given role/profile may edit
-- a given field (IAM-100+ client access profiles, IAM-030 RBAC/ABAC) is
-- enforced by the application's authorization library before the UPDATE is
-- ever issued, per IAM-034. RLS answers "can this org touch this row at
-- all," not "is this specific user allowed to do this specific thing."
create policy administration_update on administration
    for update
    using (app.has_administration_access(id))
    with check (app.has_administration_access(id));

-- DELETE: no policy, no grant. Administrations are archived via status.

-- --- firm_engagement ---------------------------------------------------------

-- SELECT: the engaged firm, or the client that owns the administration.
-- Deliberately NOT has_administration_access — that would let a second
-- firm with its own active engagement on the same administration see the
-- first firm's engagement row, which leaks who else has access to a client
-- outside of what IAM-109 promises (the client sees this; other firms do
-- not).
create policy firm_engagement_select on firm_engagement
    for select
    using (
        firm_organization_id = app.current_org_id()
        or app.is_own_administration(administration_id)
    );

-- INSERT: only the initiating party can create the row, and only as
-- 'pending' — nobody can insert a row that is already active or revoked.
-- FR-MDL-005 (firm invites) and FR-MDL-007 (client invites) are the same
-- policy, mirrored by `initiated_by`.
create policy firm_engagement_insert on firm_engagement
    for insert
    with check (
        status = 'pending'
        and (
            (initiated_by = 'firm' and firm_organization_id = app.current_org_id())
            or
            (initiated_by = 'client' and app.is_own_administration(administration_id))
        )
    );

-- UPDATE: gates WHO may touch the row at all (a participant on either
-- side). The finer question of which status transitions are legal, and
-- which specific participant may make them (only the counterparty accepts;
-- either party revokes, per FR-MDL-009) is enforced by
-- firm_engagement_transition_guard_trg below — a state machine is easier to
-- get right, and to give a useful error message for, in a trigger than as
-- a single boolean expression.
create policy firm_engagement_update on firm_engagement
    for update
    using (
        firm_organization_id = app.current_org_id()
        or app.is_own_administration(administration_id)
    )
    with check (
        firm_organization_id = app.current_org_id()
        or app.is_own_administration(administration_id)
    );

-- DELETE: no policy, no grant. Revoked/declined engagements are kept —
-- IAM-109 requires the client to see who has ever held access and since
-- when, which a deleted row cannot answer.

-- --- fiscal_year / period ---------------------------------------------------------

create policy fiscal_year_select on fiscal_year
    for select
    using (app.has_administration_access(administration_id));

create policy fiscal_year_insert on fiscal_year
    for insert
    with check (app.has_administration_access(administration_id));

create policy fiscal_year_update on fiscal_year
    for update
    using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- DELETE: no policy, no grant. Fiscal years are closed, never deleted.

create policy period_select on period
    for select
    using (app.has_administration_access(administration_id));

create policy period_insert on period
    for insert
    with check (app.has_administration_access(administration_id));

create policy period_update on period
    for update
    using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- DELETE: no policy, no grant. Periods are locked, never deleted.

-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------

-- Structural sanity on firm_engagement: the firm side must actually be
-- kind = 'firm', and an organization cannot be "engaged" with an
-- administration it already owns. Both require a cross-table lookup, which
-- a check constraint cannot express.
--
-- SECURITY DEFINER, owned by ledgr_bootstrap (BYPASSRLS) below - not
-- optional here the way it is for most triggers. ADR-002 designs
-- acceptance as something the CLIENT does (`accepted_at`, "whether the
-- client accepted"): the client's own UPDATE fires this trigger under the
-- client's tenant context, and organization_select (this file) only shows
-- a firm's row to a client once an engagement between them is already
-- active - which is exactly the row this UPDATE is trying to create. Under
-- the client's own RLS view the firm is invisible, firm_kind comes back
-- null, and a client accepting a real firm's invitation was refused with
-- "is not a firm organization" - caught by
-- tests/integration/test_authorization_isolation.py exercising the actual
-- accepting party for the first time, not the firm side this bug is
-- invisible from. This function only ever answers true/false to the guard
-- below and returns NEW unchanged; it does not expose organization.kind or
-- any other row to the caller, so bypassing RLS for this one read does not
-- widen what a client session can see.
create or replace function firm_engagement_guard() returns trigger as $$
declare
    firm_kind text;
    owning_org uuid;
begin
    select kind into firm_kind from organization where id = new.firm_organization_id;
    if firm_kind is distinct from 'firm' then
        raise exception 'firm_engagement.firm_organization_id % is not a firm organization',
            new.firm_organization_id;
    end if;

    select organization_id into owning_org
    from administration where id = new.administration_id;
    if owning_org = new.firm_organization_id then
        raise exception 'organization % already owns administration %, cannot also be engaged',
            new.firm_organization_id, new.administration_id;
    end if;

    return new;
end;
$$ language plpgsql
security definer
set search_path = public, app, pg_temp;

create trigger firm_engagement_guard_trg
    before insert or update on firm_engagement
    for each row execute function firm_engagement_guard();

-- The engagement state machine: pending -> active | declined (only by the
-- counterparty), active -> revoked (by either participant, at any time —
-- FR-MDL-009). Anything else is rejected with a specific message rather
-- than a generic constraint-violation error.
create or replace function firm_engagement_transition_guard() returns trigger as $$
declare
    v_actor_is_firm boolean := new.firm_organization_id = app.current_org_id();
    v_actor_is_client boolean := app.is_own_administration(new.administration_id);
begin
    if old.status = new.status then
        return new;
    end if;

    if old.status = 'pending' and new.status = 'active' then
        if old.initiated_by = 'firm' and not v_actor_is_client then
            raise exception 'only the client may accept a firm-initiated engagement';
        end if;
        if old.initiated_by = 'client' and not v_actor_is_firm then
            raise exception 'only the firm may accept a client-initiated engagement';
        end if;
        new.accepted_at := now();

    elsif old.status = 'pending' and new.status = 'declined' then
        if old.initiated_by = 'firm' and not v_actor_is_client then
            raise exception 'only the client may decline a firm-initiated engagement';
        end if;
        if old.initiated_by = 'client' and not v_actor_is_firm then
            raise exception 'only the firm may decline a client-initiated engagement';
        end if;

    elsif old.status = 'active' and new.status = 'revoked' then
        if not (v_actor_is_firm or v_actor_is_client) then
            raise exception 'only a participant may revoke this engagement';
        end if;
        new.revoked_at := now();

    else
        raise exception 'invalid firm_engagement status transition: % -> %', old.status, new.status;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger firm_engagement_transition_guard_trg
    before update on firm_engagement
    for each row execute function firm_engagement_transition_guard();

-- Keep the denormalized organization_id / administration_id columns on
-- fiscal_year and period in sync with their parent, always. Application
-- code never sets these directly (they aren't even meaningfully
-- overridable — WITH CHECK still requires has_administration_access to
-- hold for whatever value ends up there), so they cannot drift or be
-- forgotten.
create or replace function sync_fiscal_year_tenant_columns() returns trigger as $$
begin
    select organization_id into new.organization_id
    from administration where id = new.administration_id;
    return new;
end;
$$ language plpgsql;

create trigger fiscal_year_sync_tenant_trg
    before insert or update on fiscal_year
    for each row execute function sync_fiscal_year_tenant_columns();

create or replace function sync_period_tenant_columns() returns trigger as $$
begin
    select fy.administration_id, a.organization_id
    into new.administration_id, new.organization_id
    from fiscal_year fy
    join administration a on a.id = fy.administration_id
    where fy.id = new.fiscal_year_id;
    return new;
end;
$$ language plpgsql;

create trigger period_sync_tenant_trg
    before insert or update on period
    for each row execute function sync_period_tenant_columns();

-- ---------------------------------------------------------------------------
-- Bootstrap functions (the entire BYPASSRLS surface in this system)
-- ---------------------------------------------------------------------------
-- Owned by ledgr_bootstrap (BYPASSRLS, NOLOGIN). ledgr_app is granted
-- EXECUTE and nothing more — it can call these two functions and cannot
-- reach BYPASSRLS any other way, because no other object in the database
-- is owned by, or grants privileges equivalent to, ledgr_bootstrap.

-- Self-managed signup (Model B, FR-ONB). No tenant context exists yet —
-- that is exactly why this cannot be a normal ledgr_app INSERT: there is no
-- organization_id to satisfy organization_update_self or any other policy
-- before the row exists.
create or replace function app.signup_self_managed_organization(
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
    values ('business', p_name, p_kvk_number, now())
    returning * into v_org;
    return v_org;
end;
$$;

-- Firm creates a client administration for a business with no account yet
-- (FR-MDL-004). Creates the client organization, its administration, and an
-- already-active engagement atomically — there is no window where the
-- administration exists without an owner, or the engagement exists without
-- the administration. The calling session must already be an authenticated
-- firm (app.current_org_id() is read from the caller's own session
-- variable, not a parameter, so it cannot be spoofed by passing a
-- different firm's id).
create or replace function app.create_firm_client_administration(
    p_legal_name text,
    p_legal_form text,
    p_kvk_number text,
    p_vat_number text,
    p_invited_by_user_id uuid
) returns administration
language plpgsql
security definer
set search_path = public, app
as $$
declare
    v_firm_org_id uuid := app.current_org_id();
    v_firm_kind text;
    v_client_org organization;
    v_admin administration;
begin
    if v_firm_org_id is null then
        raise exception 'no tenant context set';
    end if;

    select kind into v_firm_kind from organization where id = v_firm_org_id;
    if v_firm_kind is distinct from 'firm' then
        raise exception 'calling organization % is not a firm', v_firm_org_id;
    end if;

    insert into organization (kind, name, claimed_at)
    values ('business', p_legal_name, null)
    returning * into v_client_org;

    insert into administration (
        organization_id, legal_name, legal_form, kvk_number, vat_number
    ) values (
        v_client_org.id, p_legal_name, p_legal_form, p_kvk_number, p_vat_number
    )
    returning * into v_admin;

    insert into firm_engagement (
        firm_organization_id, administration_id, status, initiated_by,
        invited_by_user_id, accepted_at
    ) values (
        v_firm_org_id, v_admin.id, 'active', 'firm', p_invited_by_user_id, now()
    );

    return v_admin;
end;
$$;

alter function app.signup_self_managed_organization(text, text)
    owner to ledgr_bootstrap;
alter function app.create_firm_client_administration(text, text, text, text, uuid)
    owner to ledgr_bootstrap;
-- See firm_engagement_guard()'s own comment above for why this one, alone
-- among the triggers in this file, needs to run as ledgr_bootstrap too.
alter function firm_engagement_guard() owner to ledgr_bootstrap;

revoke all on function app.signup_self_managed_organization(text, text) from public;
revoke all on function app.create_firm_client_administration(text, text, text, text, uuid) from public;

grant execute on function app.signup_self_managed_organization(text, text) to ledgr_app;
grant execute on function app.create_firm_client_administration(text, text, text, text, uuid) to ledgr_app;

commit;
