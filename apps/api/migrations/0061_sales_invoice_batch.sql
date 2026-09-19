-- 0061_sales_invoice_batch.sql
-- SI-17 (Sales Invoicing TRD): batch invoicing - generate invoices for multiple customers or
-- contracts in one pass. See docs/decisions/ADR-079-batch-invoicing.md.
--
-- ===========================================================================
-- What a batch is
-- ===========================================================================
--
-- A batch is a RECORD of one pass, not a new kind of invoice. Each entry becomes an ordinary
-- draft through `InvoicingService.create_draft` (and, if asked, an ordinary `issue`), so the
-- statutory gate, the gapless numbering, the posting and the approval gate (SI-16) are exactly
-- the ones a person meets. The tables only remember what was asked and what happened to each
-- entry, so a screen can show "37 drafted, 2 failed" and a retry can be recognised.
--
-- Written once, when the pass has finished: the counts are known then, and the rows are never
-- updated. `ledgr_app` has INSERT and SELECT only, so a batch cannot be edited or deleted -
-- what a bulk action did is what an auditor asks about.
--
-- ===========================================================================
-- Retries
-- ===========================================================================
--
-- `batch_key` is the caller's own name for the pass ("2026-09 annual fee"). A second request
-- with the same key finds the first and returns it without creating anything; the unique index
-- is what holds when two arrive at once. An invoice belongs to at most one batch item.

begin;

create table sales_invoice_batch (
    id                uuid primary key default gen_random_uuid(),
    organization_id   uuid not null references organization(id),
    administration_id uuid not null references administration(id),

    name              text not null check (length(btrim(name)) > 0),
    batch_key         text check (batch_key is null or length(btrim(batch_key)) > 0),
    invoice_date      date not null,
    issue_requested   boolean not null,

    item_count        integer not null check (item_count > 0),
    drafted_count     integer not null check (drafted_count >= 0),
    issued_count      integer not null check (issued_count >= 0),
    failed_count      integer not null check (failed_count >= 0),

    created_by_user_id uuid not null references users(id),
    created_at        timestamptz not null default now(),

    constraint sales_invoice_batch_counts_add_up check (
        drafted_count + issued_count + failed_count = item_count
    )
);

create unique index sales_invoice_batch_key_idx
    on sales_invoice_batch(administration_id, batch_key) where batch_key is not null;
create index sales_invoice_batch_administration_idx
    on sales_invoice_batch(administration_id, created_at desc);
create index sales_invoice_batch_organization_idx on sales_invoice_batch(organization_id);

create table sales_invoice_batch_item (
    id                uuid primary key default gen_random_uuid(),
    organization_id   uuid not null references organization(id),
    administration_id uuid not null references administration(id),
    batch_id          uuid not null references sales_invoice_batch(id),
    position          integer not null check (position > 0),

    customer_id       uuid not null,
    invoice_id        uuid references sales_invoice(id),

    -- drafted: a draft exists (and, when issuing was asked for, `issue_error` says why it is
    -- still a draft). issued: numbered and posted. failed: no invoice; `error_code` says why.
    status            text not null check (status in ('drafted', 'issued', 'failed')),
    error_code        text,
    issue_error       text,

    constraint sales_invoice_batch_item_shape check (
        (status = 'failed' and invoice_id is null and error_code is not null)
        or (status in ('drafted', 'issued') and invoice_id is not null and error_code is null)
    ),
    constraint sales_invoice_batch_item_issue_error_only_on_drafts check (
        issue_error is null or status = 'drafted'
    )
);

create unique index sales_invoice_batch_item_position_idx
    on sales_invoice_batch_item(batch_id, position);
-- An invoice belongs to at most one batch item.
create unique index sales_invoice_batch_item_invoice_idx
    on sales_invoice_batch_item(invoice_id) where invoice_id is not null;
create index sales_invoice_batch_item_organization_idx
    on sales_invoice_batch_item(organization_id);

comment on table sales_invoice_batch is
    'SI-17. The record of one batch-invoicing pass: what was asked and how many entries were '
    'drafted, issued or failed. Written once, never edited or deleted.';

create or replace function sales_invoice_batch_item_guard() returns trigger as $$
declare
    v_b_admin uuid;
    v_b_org   uuid;
    v_i_admin uuid;
begin
    select administration_id, organization_id into v_b_admin, v_b_org
      from sales_invoice_batch where id = new.batch_id;
    if v_b_admin is distinct from new.administration_id then
        raise exception
            'batch item belongs to administration %, but its batch belongs to % (IAM-001)',
            new.administration_id, v_b_admin;
    end if;
    if new.invoice_id is not null then
        select administration_id into v_i_admin from sales_invoice where id = new.invoice_id;
        if v_i_admin is distinct from new.administration_id then
            raise exception
                'batch item belongs to administration %, but its invoice belongs to % (IAM-001)',
                new.administration_id, v_i_admin;
        end if;
    end if;
    new.organization_id := v_b_org;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_batch_item_guard_trg
    before insert on sales_invoice_batch_item
    for each row execute function sales_invoice_batch_item_guard();

alter table sales_invoice_batch owner to ledgr_migrator;
alter table sales_invoice_batch_item owner to ledgr_migrator;

-- Insert and read only: written once, never edited or deleted.
grant select, insert on sales_invoice_batch, sales_invoice_batch_item to ledgr_app;
grant select on sales_invoice_batch, sales_invoice_batch_item to ledgr_ops;

alter table sales_invoice_batch enable row level security;
alter table sales_invoice_batch force row level security;
alter table sales_invoice_batch_item enable row level security;
alter table sales_invoice_batch_item force row level security;

create policy sales_invoice_batch_select on sales_invoice_batch
    for select using (app.has_administration_access(administration_id));
create policy sales_invoice_batch_insert on sales_invoice_batch
    for insert with check (app.has_administration_access(administration_id));
create policy sales_invoice_batch_item_select on sales_invoice_batch_item
    for select using (app.has_administration_access(administration_id));
create policy sales_invoice_batch_item_insert on sales_invoice_batch_item
    for insert with check (app.has_administration_access(administration_id));

commit;
