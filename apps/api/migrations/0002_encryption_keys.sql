-- 0002_encryption_keys.sql
-- Per-administration data encryption keys (DEKs), each wrapped by the
-- system's key-encryption key (KEK) in the managed KMS/HSM. Implements
-- SEC-022 (envelope encryption), IAM-004 (per-tenant key, so revocation
-- isolates one tenant), and the schema half of SEC-023 (rotation).
-- See docs/decisions/ADR-004-envelope-encryption.md for the key hierarchy
-- and the rotation / emergency-rotation runbook.
--
-- The tenant unit for encryption is the ADMINISTRATION, not the
-- organization: a firm organization holds many client administrations, and
-- revoking one client's key must not touch any other client's documents —
-- see ADR-004's "Alternatives considered" for why organization-level keys
-- were rejected.
--
-- The DEK's raw bytes are NEVER stored here or anywhere else at rest —
-- only the wrapped (KMS-encrypted) ciphertext. The raw DEK exists only
-- transiently, in application memory, for the duration of a single
-- encrypt/decrypt operation (see apps/api/src/api/crypto/envelope.py).

begin;

-- ---------------------------------------------------------------------------
-- ledgr_ops: the cross-tenant batch role deferred in ADR-003
-- ---------------------------------------------------------------------------
-- The annual rotation sweep and the KEK re-wrap sweep are legitimate
-- cross-tenant operations - unlike every other role in this system,
-- neither can be scoped to one organization's session. ADR-003 flagged
-- this exact need ("a fourth role for legitimate cross-tenant batch reads")
-- and deferred it; this is that role, now put to its first real use.
--
-- BYPASSRLS, but its grants are narrow on purpose: read-only on
-- `administration` (to enumerate which administrations exist), and
-- read/write only on `administration_encryption_key` (the one table these
-- jobs actually operate on). It has no access to anything else -
-- ledgr_ops bypassing RLS is scoped by grants, not by trust.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'ledgr_ops') then
        create role ledgr_ops nosuperuser nocreatedb nocreaterole bypassrls login password null;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- administration_encryption_key
-- ---------------------------------------------------------------------------
-- Versioned: rotation inserts a new row rather than mutating an existing
-- one, because old document blobs remain encrypted under old DEK versions
-- and must stay decryptable. Exactly one row per administration may be
-- 'active' (used for new writes) at a time; 'retired' rows are kept
-- indefinitely for reading old objects; 'revoked' rows are kept as a tombstone
-- (their wrapped_dek is nulled out — see administration_encryption_key_revoke
-- below) so the audit trail survives even though the key material doesn't.
create table administration_encryption_key (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    key_version         integer not null,
    wrapped_dek         bytea,              -- null once revoked; see below
    wrap_algorithm      text not null check (wrap_algorithm in ('RSA-OAEP-256', 'A256KW')),
    kek_key_id          text not null,      -- KMS key identifier + version that wrapped this DEK
    status              text not null default 'active'
                            check (status in ('active', 'retired', 'revoked')),
    rotation_reason     text not null default 'initial_provisioning'
                            check (rotation_reason in (
                                'initial_provisioning', 'scheduled_annual', 'emergency'
                            )),
    created_at          timestamptz not null default now(),
    retired_at          timestamptz,
    revoked_at          timestamptz,
    revoked_by_user_id  uuid,
    constraint administration_encryption_key_unique_version
        unique (administration_id, key_version)
);

create index administration_encryption_key_administration_idx
    on administration_encryption_key(administration_id);
create index administration_encryption_key_organization_idx
    on administration_encryption_key(organization_id);

-- Exactly one active key per administration at a time.
create unique index administration_encryption_key_one_active_idx
    on administration_encryption_key(administration_id)
    where status = 'active';

-- ---------------------------------------------------------------------------
-- Ownership, grants, RLS
-- ---------------------------------------------------------------------------
alter table administration_encryption_key owner to ledgr_migrator;

-- No DELETE grant: retired and revoked rows are kept permanently as the
-- record of what encrypted what, and when a tenant's key was cut off — the
-- same append-only discipline as the rest of this schema.
grant select, insert, update on administration_encryption_key to ledgr_app;

-- ledgr_ops: cross-tenant by BYPASSRLS, narrowly scoped by grant. Read-only
-- on administration (enumerate tenants); read/write on this table only
-- (rotate/rewrap). No DELETE here either, for the same reason as above.
grant select on administration to ledgr_ops;
grant select, insert, update on administration_encryption_key to ledgr_ops;

alter table administration_encryption_key enable row level security;
alter table administration_encryption_key force row level security;

-- SELECT/UPDATE follow the same tenant boundary as every other table here.
-- In practice, no HTTP route ever returns rows from this table — only
-- apps/api/src/api/crypto/envelope.py reads wrapped_dek, and only to pass
-- it to the KMS unwrap call. RLS is still the data-layer backstop
-- (IAM-002); "no route exposes this" is an application-level discipline on
-- top of it, not a substitute for it.
create policy administration_encryption_key_select on administration_encryption_key
    for select
    using (app.has_administration_access(administration_id));

-- INSERT: the ordinary path (self-managed administration creation, or a
-- scheduled/emergency rotation run from within the API acting in its own
-- tenant context) requires organization_id = current org, same as
-- administration_insert_own. The firm-creates-an-unclaimed-client path
-- does NOT use this policy at all — it goes through the extended
-- app.create_firm_client_administration below, which runs as
-- ledgr_bootstrap (BYPASSRLS), for the same chicken-and-egg reason
-- documented in ADR-003.
create policy administration_encryption_key_insert on administration_encryption_key
    for insert
    with check (organization_id = app.current_org_id());

create policy administration_encryption_key_update on administration_encryption_key
    for update
    using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------
create or replace function sync_administration_encryption_key_tenant_columns()
returns trigger as $$
begin
    select organization_id into new.organization_id
    from administration where id = new.administration_id;
    return new;
end;
$$ language plpgsql;

create trigger administration_encryption_key_sync_tenant_trg
    before insert or update on administration_encryption_key
    for each row execute function sync_administration_encryption_key_tenant_columns();

-- Enforces the rotation state machine at the row level, as a backstop
-- behind the application logic in envelope.py: only 'active' -> 'retired'
-- and 'active'/'retired' -> 'revoked' are legal transitions. This also
-- means a compromised or buggy application role cannot resurrect a revoked
-- key by flipping its status back — the transition simply isn't permitted.
create or replace function administration_encryption_key_transition_guard()
returns trigger as $$
begin
    if old.status = new.status then
        return new;
    end if;

    if old.status = 'active' and new.status = 'retired' then
        new.retired_at := now();
    elsif old.status in ('active', 'retired') and new.status = 'revoked' then
        new.revoked_at := now();
        new.wrapped_dek := null; -- the tombstone: material is gone, the row's history is not
    else
        raise exception 'invalid administration_encryption_key status transition: % -> %',
            old.status, new.status;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger administration_encryption_key_transition_guard_trg
    before update on administration_encryption_key
    for each row execute function administration_encryption_key_transition_guard();

-- A revoked key's wrapped_dek is null (tombstoned by the trigger above);
-- guarantee that invariant can never be violated in the other direction
-- either (no route back to having key material once revoked).
alter table administration_encryption_key
    add constraint administration_encryption_key_revoked_has_no_material
    check (status <> 'revoked' or wrapped_dek is null);

-- ---------------------------------------------------------------------------
-- Extend the firm-bootstrap function to provision the first key atomically
-- ---------------------------------------------------------------------------
-- Wrapping a DEK requires calling out to the KMS over the network, which a
-- plpgsql function cannot and should not do. So application code (see
-- apps/api/src/api/crypto/envelope.py) generates and wraps the DEK FIRST,
-- then passes the wrapped bytes in here to be persisted atomically with the
-- administration and the firm_engagement row it already created. Dropped
-- and recreated (rather than a bare CREATE OR REPLACE) because the
-- parameter list changes, which Postgres treats as a different function.
drop function if exists app.create_firm_client_administration(text, text, text, text, uuid);

create or replace function app.create_firm_client_administration(
    p_legal_name text,
    p_legal_form text,
    p_kvk_number text,
    p_vat_number text,
    p_invited_by_user_id uuid,
    p_wrapped_dek bytea,
    p_wrap_algorithm text,
    p_kek_key_id text
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

    insert into administration_encryption_key (
        organization_id, administration_id, key_version,
        wrapped_dek, wrap_algorithm, kek_key_id, status, rotation_reason
    ) values (
        v_client_org.id, v_admin.id, 1,
        p_wrapped_dek, p_wrap_algorithm, p_kek_key_id, 'active', 'initial_provisioning'
    );

    return v_admin;
end;
$$;

alter function app.create_firm_client_administration(
    text, text, text, text, uuid, bytea, text, text
) owner to ledgr_bootstrap;

revoke all on function app.create_firm_client_administration(
    text, text, text, text, uuid, bytea, text, text
) from public;

grant execute on function app.create_firm_client_administration(
    text, text, text, text, uuid, bytea, text, text
) to ledgr_app;

commit;
