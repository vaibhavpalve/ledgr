-- 0084_saved_views.sql
-- Saved views on the firm work queue: FR-FRM-000b (saved filters), FR-FRM-002. See
-- docs/decisions/ADR-115-saved-views-and-next-client.md and docs/firm-home/contract-wave2.md
-- (decision 6).
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
--   saved_view   one person's named worklist query ("Q3 VAT, mine"): the same parameters
--                GET /v1/firm/worklist takes (chip, q, assigned, sort, dir, vat_frequency), as
--                jsonb. The live count is computed per request through the worklist model; it is
--                never stored, so it can never go stale or disagree with opening the view.
--
-- A view is a label on a query, not data about any client: it holds no administration id and
-- grants nothing. It belongs to the person who saved it (`user_id`) inside the organization they
-- were working in (`organization_id`).
--
-- ===========================================================================
-- Tenancy: RLS at the organization, per-user in the repository
-- ===========================================================================
--
-- RLS is organization-level (`organization_id = app.current_org_id()`), the same predicate
-- idempotency_key (0022) uses for its per-organization rows: another organization's views are
-- invisible to the session. Within the organization, views are strictly per user; that predicate
-- (`user_id = <the verified token's user>`) is in every statement api.firm.views_repository
-- makes, and tests/integration/test_firm_views.py proves a colleague can neither list, rename nor
-- archive someone else's view.
--
-- ===========================================================================
-- Never deleted
-- ===========================================================================
--
-- Archiving sets archived_at; there is no DELETE grant. ledgr_app may UPDATE only `name`,
-- `archived_at` and `position` (column grants), and a trigger refuses un-archiving and any change
-- to who owns the row or what it queries - a view's query is fixed at save time; a different
-- query is a new view.
--
-- ===========================================================================
-- Backward compatible, and reversible (NFR-044)
-- ===========================================================================
--
-- One new table, its indexes, trigger and policies. Nothing existing changes shape, so the
-- previous application version keeps working while this is applied.
--
-- Down (documented rather than shipped as a file, matching every earlier migration here; run
-- inside one transaction, after the application no longer serves /v1/firm/views):
--   drop trigger saved_view_guard_trg on saved_view;
--   drop function saved_view_guard();
--   drop table saved_view;

begin;

create table saved_view (
    id                uuid primary key default gen_random_uuid(),
    organization_id   uuid not null references organization(id),
    user_id           uuid not null references users(id),

    name              text not null check (length(btrim(name)) between 1 and 60),
    -- The worklist query: {chip, q, assigned, sort, dir, vat_frequency}. Validated by the API
    -- (api.firm.views_model.SavedViewQuery); the database only insists it is an object.
    query             jsonb not null check (jsonb_typeof(query) = 'object'),
    position          integer not null default 0,

    created_at        timestamptz not null default now(),
    archived_at       timestamptz
);

-- The only read: one person's active views, in order.
create index saved_view_owner_idx
    on saved_view(organization_id, user_id, position)
    where archived_at is null;

comment on table saved_view is
    'FR-FRM-000b. A person''s saved firm worklist query. Per user (enforced in the repository), '
    'RLS at the organization. Archived, never deleted (ADR-115).';

create or replace function saved_view_guard() returns trigger as $$
begin
    if new.organization_id <> old.organization_id
       or new.user_id <> old.user_id
       or new.query <> old.query
       or new.created_at <> old.created_at then
        raise exception 'saved_view: only name, position and archived_at may change';
    end if;
    if old.archived_at is not null and new.archived_at is distinct from old.archived_at then
        raise exception 'saved_view: an archived view stays archived';
    end if;
    return new;
end;
$$ language plpgsql;

create trigger saved_view_guard_trg
    before update on saved_view
    for each row execute function saved_view_guard();

alter table saved_view owner to ledgr_migrator;

grant select, insert on saved_view to ledgr_app;
grant update (name, position, archived_at) on saved_view to ledgr_app;
grant select on saved_view to ledgr_ops;

alter table saved_view enable row level security;
alter table saved_view force row level security;

create policy saved_view_select on saved_view
    for select using (organization_id = app.current_org_id());
create policy saved_view_insert on saved_view
    for insert with check (organization_id = app.current_org_id());
create policy saved_view_update on saved_view
    for update using (organization_id = app.current_org_id())
    with check (organization_id = app.current_org_id());

commit;
