-- 0015_client_rights_floor.sql
-- IAM-105: "Regardless of profile, the client's Owner always retains: read
-- access to their own source documents and filed returns, export of their
-- complete data, visibility of their own audit log, the ability to revoke
-- the firm's access, and the ability to manage their own users' sign-in
-- security. No firm setting can remove these."
-- See docs/decisions/ADR-016-client-rights-floor.md.
--
-- api.authz.service.AuthorizationService.authorize() answers "yes" to those
-- six permissions for a client's Owner before anything else is consulted.
-- This migration defends the premise that answer rests on: that there IS an
-- Owner, and that their assignment still carries what it was granted with.
--
-- Three ways an Owner assignment could be quietly dismantled, and the guard
-- for each:
--   an expiry           -> refused at INSERT. An Owner grant that lapsed on
--                          a date nobody remembers would remove the floor
--                          with no action at all, which is the most silent
--                          removal available.
--   a condition         -> refused at INSERT. An ip_allowlist or amount
--                          ceiling on the Owner's own grant could withhold
--                          a floor right without ever touching a profile.
--   revoking the last   -> refused at UPDATE. An organization with no Owner
--                          has nobody holding the floor.
--
-- TRIGGERS, not policies, and that distinction is the point. Row-level
-- security is bypassed by BYPASSRLS - which ledgr_ops holds, and which the
-- operator scripts in apps/api/scripts connect as. A trigger is not:
-- Postgres runs BEFORE triggers for every writer including a superuser. So
-- these bind support tooling and a DBA at a psql prompt, not only the
-- application. That is what "no admin action can remove these" requires.
--
-- What is NOT defended here, because it is already impossible:
--   * removing a floor permission from the Owner role's bundle - ledgr_app
--     has no DELETE grant on role_permission (0009), and the role_permission
--     insert policy refuses writes to system roles either way;
--   * a client access profile withholding one - profiles cannot contain
--     organization-scope permissions at all (0013), and resolve() adds the
--     administration-scoped ones back;
--   * a firm revoking the client's Owner - role_assignment's RLS scopes
--     organization-level writes to app.current_org_id(), which for a firm
--     is the firm.

begin;

create or replace function role_assignment_owner_floor_guard() returns trigger as $$
declare
    v_is_owner boolean;
begin
    select r.is_system and r.name = 'Owner'
    into v_is_owner
    from "role" r
    where r.id = new.role_id;

    -- Only organization-scoped Owner assignments carry the floor. An
    -- administration-scoped role, or any other role, is ordinary.
    if not coalesce(v_is_owner, false) or new.scope_type <> 'organization' then
        return new;
    end if;

    if tg_op = 'INSERT' then
        if new.expires_at is not null then
            raise exception
                'an Owner assignment cannot carry an expiry (IAM-105): the client rights '
                'floor would lapse with no action and no notice';
        end if;
        if new.conditions <> '{}'::jsonb then
            raise exception
                'an Owner assignment cannot carry attribute conditions (IAM-105): a '
                'condition could withhold a floor right without touching a profile';
        end if;
        return new;
    end if;

    -- UPDATE. role_assignment_immutable_trg (0009) already rejects every
    -- field change except revocation, so this only has to consider that one.
    if old.revoked_at is null and new.revoked_at is not null then
        if not exists (
            select 1
            from role_assignment ra
            join "role" r on r.id = ra.role_id
            where ra.id <> old.id
              and ra.scope_type = 'organization'
              and ra.scope_id = old.scope_id
              and ra.revoked_at is null
              and (ra.expires_at is null or ra.expires_at > now())
              and r.is_system
              and r.name = 'Owner'
        ) then
            raise exception
                'cannot revoke the last Owner of organization % (IAM-105): the client '
                'rights floor requires an Owner to hold it - assign another Owner first',
                old.scope_id;
        end if;
    end if;

    return new;
end;
$$ language plpgsql;

-- Fires on INSERT and UPDATE. Deliberately a separate trigger from 0009's
-- role_assignment_immutable_trg rather than an extension of it: that one is
-- about IAM-032 (a grant's reach never widens), this one is about IAM-105
-- (a floor never disappears), and folding them together would make either
-- requirement harder to find and easier to break while editing the other.
create trigger role_assignment_owner_floor_guard_trg
    before insert or update on role_assignment
    for each row execute function role_assignment_owner_floor_guard();

-- ---------------------------------------------------------------------------
-- A readable statement of the floor, for the audit report IAM-105 implies
-- ---------------------------------------------------------------------------
-- Not consulted when authorizing - api.authz.rights_floor is the source of
-- truth for that, and a table the application read would be one more thing
-- that could be edited to shrink the floor. This exists so an auditor
-- querying the database can see what the floor IS, in the PRD's own words,
-- without reading Python.
create table client_rights_floor_statement (
    action          text not null,
    resource_type   text not null,
    resource_scope  text not null check (resource_scope in ('organization', 'administration')),
    phrase          text not null,
    primary key (action, resource_type)
);

insert into client_rights_floor_statement (action, resource_type, resource_scope, phrase) values
    ('view',   'document',        'administration', 'read access to their own source documents'),
    ('view',   'vat_return',      'administration', 'read access to their filed returns'),
    ('export', 'report_data',     'administration', 'export of their complete data'),
    ('read',   'audit_log',       'administration', 'visibility of their own audit log'),
    ('revoke', 'firm_engagement', 'organization',   'the ability to revoke the firm''s access'),
    ('manage', 'security_policy', 'organization',
        'the ability to manage their own users'' sign-in security');

alter table client_rights_floor_statement owner to ledgr_migrator;

-- SELECT only, for everyone. There is no write path to this table from the
-- application at all: the floor is not configurable, so nothing that could
-- edit it should exist.
grant select on client_rights_floor_statement to ledgr_app;

alter table client_rights_floor_statement enable row level security;
alter table client_rights_floor_statement force row level security;

-- Global reference data with no tenant dimension - the floor is identical
-- for every client. Stated explicitly rather than left unprotected, the
-- same treatment the permission catalogue gets in 0009.
create policy client_rights_floor_statement_select on client_rights_floor_statement
    for select using (true);

commit;
