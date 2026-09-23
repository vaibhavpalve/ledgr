-- 0064_fixed_assets.sql
-- The Assets screen (`/assets`) - PRD §13's fixed-asset register, ungraded by Appendix A but
-- named by the gap analysis against Exact Online/Yuki as one of the four screens with nothing
-- behind them. See docs/decisions/ADR-083-fixed-asset-register-and-depreciation.md.
--
-- ===========================================================================
-- What is here
-- ===========================================================================
--
--   fixed_asset                  the register: one row per asset, its cost, its useful life, and
--                                 the three ledger accounts a depreciation/disposal posting needs.
--   fixed_asset_depreciation_run one posted depreciation entry, once per (asset, period).
--
-- Depreciation and disposal are POSTED THE ORDINARY WAY: `api.assets.service` calls
-- `LedgerService.post()`, the same public entry point expense and invoice posting already use.
-- Nothing here is a `ledger.*` SECURITY DEFINER function and neither table is owned by
-- `ledgr_ledger` - a fixed asset is master data (like a customer), not part of the ledger's own
-- bounded context. The actual posting still goes through the one write path CLAUDE.md's first
-- non-negotiable names; this migration adds no second one.
--
-- ===========================================================================
-- Why "once per (asset, period)" is a unique index and not a guideline
-- ===========================================================================
--
-- A depreciation run's whole job is to be idempotent: posting the same asset's September charge
-- twice would double the expense and understate the asset's book value for no reason anyone
-- intended. `fixed_asset_depreciation_run_once_idx` makes a second attempt for the same period
-- fail at the database rather than depend on the caller checking first.
--
-- ===========================================================================
-- Disposal is a status, not a delete
-- ===========================================================================
--
-- A disposed asset keeps its whole depreciation history - CMP-009's shape applied to a register
-- rather than a ledger. There is no DELETE grant; `ledgr_app` cannot remove a row that a posted
-- depreciation run points at.

begin;

create table fixed_asset (
    id                                   uuid primary key default gen_random_uuid(),
    organization_id                      uuid not null references organization(id),
    administration_id                    uuid not null references administration(id),

    name                                 text not null check (length(btrim(name)) > 0),
    category                             text,

    acquisition_date                     date not null,
    acquisition_cost                     numeric(19, 2) not null check (acquisition_cost > 0),
    residual_value                       numeric(19, 2) not null default 0
                                             check (residual_value >= 0),
    useful_life_months                   integer not null check (useful_life_months > 0),
    depreciation_method                  text not null default 'straight_line'
                                             check (depreciation_method in ('straight_line')),

    -- The three accounts a depreciation posting needs (Dr expense, Cr accumulated
    -- depreciation) and the asset control account a disposal posting clears. All three are
    -- this administration's own - fixed_asset_accounts_belong_here below is what checks it,
    -- because a foreign key alone cannot compare two tables' administration_id.
    asset_account_id                     uuid not null references ledger_account(id),
    depreciation_expense_account_id      uuid not null references ledger_account(id),
    accumulated_depreciation_account_id  uuid not null references ledger_account(id),

    status                               text not null default 'active'
                                             check (status in ('active', 'disposed')),
    disposal_date                        date,
    disposal_proceeds                    numeric(19, 2),
    disposal_journal_entry_id            uuid references journal_entry(id),

    created_at                           timestamptz not null default now(),
    created_by_user_id                   uuid references users(id),
    updated_at                           timestamptz not null default now(),

    constraint fixed_asset_residual_not_above_cost check (residual_value <= acquisition_cost),
    constraint fixed_asset_disposal_is_complete check (
        (status = 'disposed') = (disposal_date is not null)
    ),
    constraint fixed_asset_disposal_proceeds_needs_disposal check (
        disposal_proceeds is null or status = 'disposed'
    )
);

create index fixed_asset_administration_idx on fixed_asset(administration_id, status);
create index fixed_asset_organization_idx on fixed_asset(organization_id);

comment on table fixed_asset is
    'PRD §13''s fixed-asset register. Depreciation and disposal are posted through '
    'LedgerService, the ordinary way - this table carries no posting privilege of its own.';

create or replace function fixed_asset_accounts_belong_here() returns trigger as $$
declare
    v_mismatch text;
begin
    select a.code into v_mismatch
      from ledger_account a
     where a.id in (new.asset_account_id, new.depreciation_expense_account_id,
                    new.accumulated_depreciation_account_id)
       and a.administration_id <> new.administration_id
     limit 1;
    if v_mismatch is not null then
        raise exception
            'account % belongs to another administration; a fixed asset''s accounts must be '
            'this administration''s own', v_mismatch;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger fixed_asset_accounts_belong_here_trg
    before insert or update on fixed_asset
    for each row execute function fixed_asset_accounts_belong_here();

create or replace function fixed_asset_touch_updated_at() returns trigger as $$
begin
    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger fixed_asset_touch_updated_at_trg
    before update on fixed_asset
    for each row execute function fixed_asset_touch_updated_at();

alter table fixed_asset owner to ledgr_migrator;

-- No DELETE: a disposed asset keeps its history, the same posture 0039 takes on `customer`.
grant select, insert, update on fixed_asset to ledgr_app;
grant select on fixed_asset to ledgr_ops;

alter table fixed_asset enable row level security;
alter table fixed_asset force row level security;

create policy fixed_asset_select on fixed_asset
    for select using (app.has_administration_access(administration_id));
create policy fixed_asset_insert on fixed_asset
    for insert with check (app.has_administration_access(administration_id));
create policy fixed_asset_update on fixed_asset
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- ===========================================================================
-- fixed_asset_depreciation_run - one posted charge per (asset, period)
-- ===========================================================================
create table fixed_asset_depreciation_run (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references organization(id),
    administration_id    uuid not null references administration(id),
    fixed_asset_id       uuid not null references fixed_asset(id),
    period_id            uuid not null references period(id),
    amount               numeric(19, 2) not null check (amount >= 0),
    journal_entry_id     uuid not null references journal_entry(id),
    posted_at            timestamptz not null default now(),
    posted_by_user_id    uuid references users(id)
);

-- The guarantee this table exists for: a second attempt at the same asset's same period fails
-- here rather than doubling the expense.
create unique index fixed_asset_depreciation_run_once_idx
    on fixed_asset_depreciation_run(fixed_asset_id, period_id);

create index fixed_asset_depreciation_run_asset_idx
    on fixed_asset_depreciation_run(fixed_asset_id, posted_at);
create index fixed_asset_depreciation_run_administration_idx
    on fixed_asset_depreciation_run(administration_id);

comment on table fixed_asset_depreciation_run is
    'One posted depreciation entry per (fixed_asset, period). Insert-only: a depreciation run, '
    'once posted, is corrected the way any posting is - a reversing entry - not edited here.';

alter table fixed_asset_depreciation_run owner to ledgr_migrator;

-- Insert-only: no UPDATE, no DELETE. The posting it points at is immutable (FR-GL-003); a record
-- of having posted it should be no less so.
grant select, insert on fixed_asset_depreciation_run to ledgr_app;
grant select on fixed_asset_depreciation_run to ledgr_ops;

alter table fixed_asset_depreciation_run enable row level security;
alter table fixed_asset_depreciation_run force row level security;

create policy fixed_asset_depreciation_run_select on fixed_asset_depreciation_run
    for select using (app.has_administration_access(administration_id));
create policy fixed_asset_depreciation_run_insert on fixed_asset_depreciation_run
    for insert with check (app.has_administration_access(administration_id));

-- ===========================================================================
-- The permissions, for databases that predate them
-- ===========================================================================
-- 0010 is GENERATED from api.authz.matrix and regenerated in place, so a fresh database gets
-- both rows there. A database that already ran an older 0010 does not, so they are added here,
-- idempotently (a fresh database finds every row already present and does nothing) - the same
-- shape 0060 used for `approve sales_invoice`.
insert into permission (action, resource_type, resource_scope, description)
select 'view', 'fixed_asset', 'administration', 'View fixed assets'
 where not exists (
     select 1 from permission where action = 'view' and resource_type = 'fixed_asset'
 );
insert into permission (action, resource_type, resource_scope, description)
select 'manage', 'fixed_asset', 'administration', 'Create, depreciate and dispose fixed assets'
 where not exists (
     select 1 from permission where action = 'manage' and resource_type = 'fixed_asset'
 );

insert into role_permission (role_id, permission_id)
select r.id, p.id
  from "role" r, permission p
 where r.is_system
   and r.name in ('Owner', 'Accountant', 'Bookkeeper')
   and p.action = 'view' and p.resource_type = 'fixed_asset'
   and not exists (
       select 1 from role_permission rp where rp.role_id = r.id and rp.permission_id = p.id
   );
insert into role_permission (role_id, permission_id)
select r.id, p.id
  from "role" r, permission p
 where r.is_system
   and r.name = 'Viewer'
   and p.action = 'view' and p.resource_type = 'fixed_asset'
   and not exists (
       select 1 from role_permission rp where rp.role_id = r.id and rp.permission_id = p.id
   );
insert into role_permission (role_id, permission_id)
select r.id, p.id
  from "role" r, permission p
 where r.is_system
   and r.name in ('Owner', 'Accountant', 'Bookkeeper')
   and p.action = 'manage' and p.resource_type = 'fixed_asset'
   and not exists (
       select 1 from role_permission rp where rp.role_id = r.id and rp.permission_id = p.id
   );

commit;
