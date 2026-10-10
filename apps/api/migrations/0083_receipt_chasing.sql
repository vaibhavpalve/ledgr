-- 0083_receipt_chasing.sql
-- Firm home wave 2, decision 5: chasing a client for missing receipts by e-mail - opt-in per
-- client, sent by an ops sweep at most once per cadence window, plus a manual bulk request.
-- See docs/decisions/ADR-114-receipt-chasing.md. Requirements: FR-NTF-001, FR-NTF-004 (no
-- amounts or counterparties in the message), FR-FRM-002.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
--   chase_setting          whether a client is chased and how often. APPEND-ONLY: the latest
--                          row per administration wins; switching chasing off is a new row.
--   chase_request          a firm user's "Request missing receipts" for one client. APPEND-ONLY.
--                          The firm's session cannot see who the client's users are (0009's RLS
--                          shows an organization's grants only to that organization, ADR-111),
--                          so the request is recorded here and the sweep delivers it.
--   chase_send             one row per chase e-mail round actually SENT (scheduled or manual),
--                          with how many lines were missing and how many people were mailed.
--                          APPEND-ONLY. Written by the sweep in the client's own tenant context.
--                          Two partial unique indexes make the sweep's "once" a database fact:
--                          one scheduled send per (administration, cadence window), one send per
--                          manual request.
--   chase_recipient_count  a CACHE (not a record) of how many of the client's people a chase
--                          would reach, refreshed by the sweep, read by the firm's preview. The
--                          one table here whose rows are updated; only the owning organization's
--                          session (the sweep's) may write it.
--
-- Nothing here holds an amount, a counterparty or a document: only counts. No posting table is
-- read or written.
--
-- ===========================================================================
-- Tenancy
-- ===========================================================================
--
-- Every row carries organization_id (the administration's OWNING organization, checked by
-- trigger) and administration_id. SELECT on all four is app.has_administration_access - the
-- owner, or a firm with an active engagement. INSERT on chase_setting and chase_request is the
-- same (the firm opts its client in, and requests). INSERT on chase_send and writes to
-- chase_recipient_count additionally require organization_id = app.current_org_id(): only a
-- session in the client's own tenant context - the sweep's - can record a send, so a firm
-- session cannot forge one.
--
-- Backward compatible: four new tables and grants only; the previous application version never
-- reads them. Safe to apply while it is serving.
--
-- Down (documented rather than shipped as a file, matching every earlier migration here; run
-- inside one transaction, after the application no longer serves the chasing routes and the
-- chase sweep cron is stopped):
--   revoke select (administration_id, status) on firm_engagement from ledgr_ops;
--   drop table chase_recipient_count;
--   drop table chase_send;
--   drop table chase_request;
--   drop table chase_setting;
--   drop function chase_row_guard();

begin;

-- ---------------------------------------------------------------------------
-- The shared row guard: organization_id is the administration's owner; a send names its own
-- request's administration.
-- ---------------------------------------------------------------------------
create or replace function chase_row_guard() returns trigger as $$
declare
    v_owner uuid;
    v_request_admin uuid;
begin
    select organization_id into v_owner from administration where id = new.administration_id;
    if v_owner is null then
        raise exception 'administration % is not a visible administration', new.administration_id;
    end if;
    if v_owner <> new.organization_id then
        raise exception '%.organization_id must be the administration''s owner', tg_table_name;
    end if;
    -- Nested, not `and`: plpgsql resolves new.request_id even when the first operand is false,
    -- and the other three tables have no such column.
    if tg_table_name = 'chase_send' then
        if new.request_id is not null then
            select administration_id into v_request_admin
              from chase_request where id = new.request_id;
            if v_request_admin is distinct from new.administration_id then
                raise exception 'chase_send names a request of another administration';
            end if;
        end if;
    end if;
    if tg_op = 'UPDATE' then
        if new.administration_id is distinct from old.administration_id
           or new.organization_id is distinct from old.organization_id then
            raise exception '% : administration and organization are immutable', tg_table_name;
        end if;
    end if;
    return new;
end;
$$ language plpgsql;

-- ---------------------------------------------------------------------------
-- chase_setting (append-only, latest row wins)
-- ---------------------------------------------------------------------------
create table chase_setting (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organization(id),
    administration_id  uuid not null references administration(id),
    enabled            boolean not null,
    cadence            text not null default 'weekly' check (cadence in ('weekly', 'fortnightly')),
    set_by_user_id     uuid not null references users(id),
    created_at         timestamptz not null default now()
);

create index chase_setting_latest_idx on chase_setting(administration_id, created_at desc, id desc);

comment on table chase_setting is
    'ADR-114. Receipt chasing opt-in per administration. Append-only; the latest row wins.';

create trigger chase_setting_guard_trg
    before insert on chase_setting
    for each row execute function chase_row_guard();

-- ---------------------------------------------------------------------------
-- chase_request (append-only)
-- ---------------------------------------------------------------------------
create table chase_request (
    id                    uuid primary key default gen_random_uuid(),
    organization_id       uuid not null references organization(id),
    administration_id     uuid not null references administration(id),
    requested_by_user_id  uuid not null references users(id),
    missing_count         integer not null check (missing_count > 0),
    requested_at          timestamptz not null default now()
);

create index chase_request_latest_idx on chase_request(administration_id, requested_at desc);
-- The sweep's "requests of the last day" scan.
create index chase_request_recent_idx on chase_request(requested_at);

comment on table chase_request is
    'ADR-114. A firm user''s manual "Request missing receipts" for one client; the sweep sends it.';

create trigger chase_request_guard_trg
    before insert on chase_request
    for each row execute function chase_row_guard();

-- ---------------------------------------------------------------------------
-- chase_send (append-only)
-- ---------------------------------------------------------------------------
create table chase_send (
    id                    uuid primary key default gen_random_uuid(),
    organization_id       uuid not null references organization(id),
    administration_id     uuid not null references administration(id),
    kind                  text not null check (kind in ('scheduled', 'manual')),
    missing_count         integer not null check (missing_count > 0),
    recipient_count       integer not null check (recipient_count > 0),
    requested_by_user_id  uuid references users(id),
    request_id            uuid references chase_request(id),
    -- The cadence window the send falls in ('2026-W41' weekly, '2026-F21' fortnightly), in
    -- Europe/Amsterdam time. A manual send carries one too, so the scheduled send of the same
    -- window is skipped.
    window_key            text not null check (length(btrim(window_key)) > 0),
    sent_at               timestamptz not null default now(),

    constraint chase_send_manual_has_request check (
        (kind = 'manual') = (request_id is not null)
        and (kind = 'manual') = (requested_by_user_id is not null)
    )
);

create unique index chase_send_one_per_window_idx
    on chase_send(administration_id, window_key) where kind = 'scheduled';
create unique index chase_send_one_per_request_idx
    on chase_send(request_id) where request_id is not null;
-- last_chased_at per administration (the worklist row, the preview).
create index chase_send_latest_idx on chase_send(administration_id, sent_at desc);

comment on table chase_send is
    'ADR-114. Every receipt-chasing e-mail round sent. Append-only; written by the chase sweep in '
    'the client''s own tenant context; counts only, never an amount or counterparty.';

create trigger chase_send_guard_trg
    before insert on chase_send
    for each row execute function chase_row_guard();

-- ---------------------------------------------------------------------------
-- chase_recipient_count (a cache, written by the sweep)
-- ---------------------------------------------------------------------------
create table chase_recipient_count (
    administration_id  uuid primary key references administration(id),
    organization_id    uuid not null references organization(id),
    recipient_count    integer not null check (recipient_count >= 0),
    counted_at         timestamptz not null default now()
);

create index chase_recipient_count_age_idx on chase_recipient_count(counted_at);

comment on table chase_recipient_count is
    'ADR-114. Cache: how many of the client''s people a chase would reach. Refreshed by the '
    'sweep in the client''s tenant context; the firm''s preview reads it.';

create trigger chase_recipient_count_guard_trg
    before insert or update on chase_recipient_count
    for each row execute function chase_row_guard();

-- ---------------------------------------------------------------------------
-- Ownership, grants, RLS
-- ---------------------------------------------------------------------------
alter table chase_setting         owner to ledgr_migrator;
alter table chase_request         owner to ledgr_migrator;
alter table chase_send            owner to ledgr_migrator;
alter table chase_recipient_count owner to ledgr_migrator;

-- Append-only: no UPDATE or DELETE on the three records, to anyone.
grant select, insert on chase_setting to ledgr_app;
grant select, insert on chase_request to ledgr_app;
grant select, insert on chase_send to ledgr_app;
grant select, insert, update on chase_recipient_count to ledgr_app;

-- The sweep's discovery pass runs as ledgr_ops (ADR-090's posture): which clients are opted in,
-- which requests are waiting, which counts are stale, and which administrations a firm is engaged
-- on. Everything it WRITES, it writes as ledgr_app in the client's own tenant context
-- (api.firm.proposals_job's two-role shape), so ledgr_ops gets SELECT only.
grant select on chase_setting, chase_request, chase_send, chase_recipient_count to ledgr_ops;
grant select (administration_id, status) on firm_engagement to ledgr_ops;

alter table chase_setting         enable row level security;
alter table chase_setting         force row level security;
alter table chase_request         enable row level security;
alter table chase_request         force row level security;
alter table chase_send            enable row level security;
alter table chase_send            force row level security;
alter table chase_recipient_count enable row level security;
alter table chase_recipient_count force row level security;

create policy chase_setting_select on chase_setting
    for select using (app.has_administration_access(administration_id));
create policy chase_setting_insert on chase_setting
    for insert with check (app.has_administration_access(administration_id));

create policy chase_request_select on chase_request
    for select using (app.has_administration_access(administration_id));
create policy chase_request_insert on chase_request
    for insert with check (app.has_administration_access(administration_id));

create policy chase_send_select on chase_send
    for select using (app.has_administration_access(administration_id));
create policy chase_send_insert on chase_send
    for insert with check (
        organization_id = app.current_org_id()
        and app.has_administration_access(administration_id)
    );

create policy chase_recipient_count_select on chase_recipient_count
    for select using (app.has_administration_access(administration_id));
create policy chase_recipient_count_insert on chase_recipient_count
    for insert with check (
        organization_id = app.current_org_id()
        and app.has_administration_access(administration_id)
    );
create policy chase_recipient_count_update on chase_recipient_count
    for update using (organization_id = app.current_org_id())
    with check (
        organization_id = app.current_org_id()
        and app.has_administration_access(administration_id)
    );

commit;
