-- 0013_client_access_profiles.sql
-- IAM-100 through IAM-104 (PRD §8.6, "Firm control of client access"). See
-- docs/decisions/ADR-015-client-access-profiles.md.
--
-- In Model A the firm decides what the CLIENT's own users may do inside
-- their administration. A profile is a cap, not a grant: it never gives a
-- client user a permission their role does not already carry, it only
-- withholds. That asymmetry is why a profile is safe to apply on top of the
-- ordinary role model and why IAM-102's ceiling is the only escalation
-- surface worth guarding.
--
-- Four tables:
--   client_access_profile          the named, reusable thing a firm selects
--   client_access_profile_version  IAM-100's "versioned" - append-only, one
--                                  row per published edit, carrying the
--                                  IAM-103 plain-language summary
--   client_access_profile_permission  a version's permitted set
--   administration_access_profile  which profile governs which client
--                                  administration, append-only with a
--                                  superseded_at, so IAM-111's audit trail
--                                  has the history and not just the present
--
-- IAM-104's per-profile restrictions live on the version as JSONB and are
-- COMPILED DOWN to the existing model at resolution time: the booleans
-- withhold permissions, and journal/amount limits become ordinary IAM-033
-- attribute conditions. There is deliberately no second enforcement path.

begin;

-- ---------------------------------------------------------------------------
-- client_access_profile
-- ---------------------------------------------------------------------------
create table client_access_profile (
    id                      uuid primary key default gen_random_uuid(),
    -- NULL for the three built-ins IAM-101 ships; set for a firm's own.
    firm_organization_id    uuid references organization(id),
    name                    text not null,
    description             text not null default '',
    is_builtin              boolean not null default false,
    created_by_user_id      uuid references users(id),
    archived_at             timestamptz,
    created_at              timestamptz not null default now(),
    constraint profile_builtin_has_no_firm check (
        (is_builtin and firm_organization_id is null)
        or (not is_builtin and firm_organization_id is not null)
    )
);

create unique index client_access_profile_builtin_name_idx
    on client_access_profile(name) where is_builtin;
create unique index client_access_profile_firm_name_idx
    on client_access_profile(firm_organization_id, name) where not is_builtin;

-- ---------------------------------------------------------------------------
-- client_access_profile_version
-- ---------------------------------------------------------------------------
-- IAM-100's "versioned". Append-only: editing a profile publishes a new
-- version rather than mutating the current one, so the exact rules that were
-- in force during any past period remain reconstructable - which is what an
-- auditor asking "what could this client do in March" needs.
--
-- An assignment points at the PROFILE, not at a version, so a newly
-- published version takes effect for every administration using that profile.
-- That is IAM-103's "profile changes take effect within 60 seconds" and
-- IAM-100's "reusable across clients" working together; pinning an
-- assignment to a version would defeat both.
create table client_access_profile_version (
    id                  uuid primary key default gen_random_uuid(),
    profile_id          uuid not null references client_access_profile(id),
    version             integer not null check (version > 0),
    -- IAM-103: "the client's Owner is notified with a plain-language summary
    -- of what changed". Stored on the version that caused the change, so the
    -- summary and the change can never drift apart.
    summary             text not null check (length(btrim(summary)) > 0),
    restrictions        jsonb not null default '{}',
    published_by_user_id uuid references users(id),
    published_at        timestamptz not null default now(),
    constraint profile_version_unique unique (profile_id, version)
);

create index client_access_profile_version_profile_idx
    on client_access_profile_version(profile_id, version desc);

create or replace function client_access_profile_version_immutable() returns trigger as $$
begin
    raise exception
        'a published profile version is immutable (IAM-100) - publish a new version instead';
end;
$$ language plpgsql;

create trigger client_access_profile_version_immutable_trg
    before update on client_access_profile_version
    for each row execute function client_access_profile_version_immutable();

-- IAM-104's closed set. Same discipline as role_assignment's conditions
-- (0009): an unrecognized key would be silently ignored by a naive
-- resolver, turning a typo into an unrestricted profile.
create or replace function client_access_profile_restrictions_guard() returns trigger as $$
declare
    v_key text;
begin
    for v_key in select jsonb_object_keys(new.restrictions)
    loop
        if v_key not in (
            'visible_journal_ids', 'bank_detail_visible', 'reports_visible',
            'periods_editable', 'approval_amount_ceiling'
        ) then
            raise exception
                'unknown profile restriction %; IAM-104''s set is closed '
                '(visible_journal_ids, bank_detail_visible, reports_visible, '
                'periods_editable, approval_amount_ceiling)', v_key;
        end if;
    end loop;

    if new.restrictions ? 'approval_amount_ceiling' then
        if jsonb_typeof(new.restrictions -> 'approval_amount_ceiling') <> 'string' then
            raise exception
                'approval_amount_ceiling must be a decimal string, not a JSON number '
                '(NFR-031: no financial calculation uses floating point)';
        end if;
        perform (new.restrictions ->> 'approval_amount_ceiling')::numeric;
    end if;

    if new.restrictions ? 'visible_journal_ids'
        and jsonb_typeof(new.restrictions -> 'visible_journal_ids') <> 'array' then
        raise exception 'visible_journal_ids must be a JSON array';
    end if;

    foreach v_key in array array['bank_detail_visible', 'reports_visible', 'periods_editable']
    loop
        if new.restrictions ? v_key and jsonb_typeof(new.restrictions -> v_key) <> 'boolean' then
            raise exception 'profile restriction % must be a boolean', v_key;
        end if;
    end loop;

    return new;
end;
$$ language plpgsql;

create trigger client_access_profile_restrictions_guard_trg
    before insert on client_access_profile_version
    for each row execute function client_access_profile_restrictions_guard();

-- ---------------------------------------------------------------------------
-- client_access_profile_permission
-- ---------------------------------------------------------------------------
create table client_access_profile_permission (
    profile_version_id  uuid not null references client_access_profile_version(id),
    permission_id       uuid not null references permission(id),
    primary key (profile_version_id, permission_id)
);

-- A profile governs what a client may do INSIDE one administration. An
-- organization-scope permission is not a profile's to give or withhold -
-- IAM-105's floor puts several of them permanently beyond a firm's reach
-- (revoking the firm's own access, managing the client's sign-in security),
-- and a profile that appeared to control them would be misleading about
-- what it does.
create or replace function client_access_profile_permission_guard() returns trigger as $$
declare
    v_scope text;
begin
    select resource_scope into v_scope from permission where id = new.permission_id;
    if v_scope = 'organization' then
        raise exception
            'a client access profile cannot contain the organization-scope permission % - '
            'profiles govern access within one administration (IAM-100)', new.permission_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger client_access_profile_permission_guard_trg
    before insert on client_access_profile_permission
    for each row execute function client_access_profile_permission_guard();

-- ---------------------------------------------------------------------------
-- administration_access_profile
-- ---------------------------------------------------------------------------
-- Append-only with a superseded_at rather than an updatable row: IAM-111
-- requires every change to appear in both audit logs, and a row that is
-- overwritten cannot answer "which profile governed this administration in
-- March".
create table administration_access_profile (
    id                      uuid primary key default gen_random_uuid(),
    administration_id       uuid not null references administration(id),
    profile_id              uuid not null references client_access_profile(id),
    assigned_by_user_id     uuid not null references users(id),
    assigned_at             timestamptz not null default now(),
    superseded_at           timestamptz
);

-- IAM-100: "For each managed administration the firm selects A client
-- access profile" - exactly one in force at a time.
create unique index administration_access_profile_current_idx
    on administration_access_profile(administration_id)
    where superseded_at is null;

create index administration_access_profile_profile_idx
    on administration_access_profile(profile_id) where superseded_at is null;

create or replace function administration_access_profile_immutable() returns trigger as $$
begin
    if new.administration_id is distinct from old.administration_id
        or new.profile_id is distinct from old.profile_id
        or new.assigned_by_user_id is distinct from old.assigned_by_user_id
        or new.assigned_at is distinct from old.assigned_at
    then
        raise exception
            'a profile assignment is immutable - supersede it with a new assignment instead';
    end if;
    if old.superseded_at is not null and new.superseded_at is distinct from old.superseded_at then
        raise exception 'a superseded profile assignment cannot be reinstated or re-dated';
    end if;
    return new;
end;
$$ language plpgsql;

create trigger administration_access_profile_immutable_trg
    before update on administration_access_profile
    for each row execute function administration_access_profile_immutable();

-- ---------------------------------------------------------------------------
-- Ownership, grants, RLS
-- ---------------------------------------------------------------------------
alter table client_access_profile             owner to ledgr_migrator;
alter table client_access_profile_version     owner to ledgr_migrator;
alter table client_access_profile_permission  owner to ledgr_migrator;
alter table administration_access_profile     owner to ledgr_migrator;

grant select, insert, update on client_access_profile to ledgr_app;
grant select, insert on client_access_profile_version to ledgr_app;
grant select, insert on client_access_profile_permission to ledgr_app;
grant select, insert, update on administration_access_profile to ledgr_app;

alter table client_access_profile enable row level security;
alter table client_access_profile force row level security;
alter table client_access_profile_version enable row level security;
alter table client_access_profile_version force row level security;
alter table client_access_profile_permission enable row level security;
alter table client_access_profile_permission force row level security;
alter table administration_access_profile enable row level security;
alter table administration_access_profile force row level security;

-- A profile is visible to the firm that owns it, to anyone for the
-- built-ins, and - importantly - to a CLIENT whose administration it
-- governs. IAM-103 forbids silent reduction of a client's access, which a
-- client who cannot see the profile capping them could not detect.
create policy client_access_profile_select on client_access_profile
    for select
    using (
        is_builtin
        or firm_organization_id = app.current_org_id()
        or exists (
            select 1
            from administration_access_profile aap
            join administration a on a.id = aap.administration_id
            where aap.profile_id = client_access_profile.id
              and aap.superseded_at is null
              and a.organization_id = app.current_org_id()
        )
    );

create policy client_access_profile_insert on client_access_profile
    for insert
    with check (not is_builtin and firm_organization_id = app.current_org_id());

-- Archiving only; nothing else about a profile changes after creation.
create policy client_access_profile_update on client_access_profile
    for update
    using (not is_builtin and firm_organization_id = app.current_org_id())
    with check (not is_builtin and firm_organization_id = app.current_org_id());

create policy client_access_profile_version_select on client_access_profile_version
    for select
    using (
        exists (
            select 1 from client_access_profile p
            where p.id = client_access_profile_version.profile_id
        )
    );

create policy client_access_profile_version_insert on client_access_profile_version
    for insert
    with check (
        exists (
            select 1 from client_access_profile p
            where p.id = client_access_profile_version.profile_id
              and not p.is_builtin
              and p.firm_organization_id = app.current_org_id()
        )
    );

create policy client_access_profile_permission_select on client_access_profile_permission
    for select
    using (
        exists (
            select 1 from client_access_profile_version v
            where v.id = client_access_profile_permission.profile_version_id
        )
    );

create policy client_access_profile_permission_insert on client_access_profile_permission
    for insert
    with check (
        exists (
            select 1
            from client_access_profile_version v
            join client_access_profile p on p.id = v.profile_id
            where v.id = client_access_profile_permission.profile_version_id
              and not p.is_builtin
              and p.firm_organization_id = app.current_org_id()
        )
    );

-- Visible to the client whose administration it governs and to a firm with
-- access to it; assignable only by a party with access to the administration.
create policy administration_access_profile_select on administration_access_profile
    for select using (app.has_administration_access(administration_id));

create policy administration_access_profile_insert on administration_access_profile
    for insert with check (app.has_administration_access(administration_id));

create policy administration_access_profile_update on administration_access_profile
    for update
    using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;
