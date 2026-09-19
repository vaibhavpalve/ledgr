-- 0056_recurring_invoices.sql
-- FR-AR-008 (PRD §6.3), SI-07. See docs/decisions/ADR-074-recurring-invoices.md.
--
--   FR-AR-008  Recurring/subscription invoicing with schedules, indexation and end
--              dates.
--
-- ===========================================================================
-- A schedule is a TEMPLATE, and an invoice is still an invoice
-- ===========================================================================
--
-- Nothing here writes an invoice, a posting or a number. A schedule says "this
-- customer, these lines, on this rhythm"; generating one is `InvoicingService.
-- create_draft` (and, if the schedule asks, `issue`), exactly as if a person had
-- done it. So FR-AR-003's statutory gate, FR-AR-004's gapless numbering and
-- FR-GL-006's posting are not re-implemented here and cannot be bypassed by a
-- schedule.
--
-- Three tables:
--
--   recurring_invoice        the schedule: customer, rhythm, end, indexation
--   recurring_invoice_line   what it bills (the same shape as an invoice line)
--   recurring_invoice_run    one row per invoice a schedule has generated
--
-- ===========================================================================
-- Idempotency is the run table's unique key
-- ===========================================================================
--
-- `unique (recurring_invoice_id, run_date)`: a schedule generates at most one
-- invoice per scheduled date, ever. A retried request, a double click and two
-- workers racing all reach the same constraint, and the second one fails rather
-- than billing the customer twice (NFR-032). The generator writes the draft and
-- the run row in one savepoint, so a losing racer's draft is rolled back with it.
--
-- ===========================================================================
-- Dates are derived from the anchor, not chained
-- ===========================================================================
--
-- `next_run_on` is stored (the run query needs an index on it) but it is always
-- `occurrence(start_date, interval, runs_generated)`, never "the previous date
-- plus a month". Chaining drifts: a schedule anchored on 31 January would go
-- 28 Feb, 28 Mar, 28 Apr; anchored, it goes 28 Feb, 31 Mar, 30 Apr. The rule lives
-- in `api.invoicing.recurrence`; this table just holds the results.
--
-- ===========================================================================
-- Indexation is an annual percentage, applied on each anniversary
-- ===========================================================================
--
-- `indexation_percent` raises every unit price by that percentage once per
-- COMPLETED year since `start_date`, compounding. The base price is stored
-- unindexed and the indexed price is computed at generation, so the schedule
-- always shows what was agreed and each invoice shows what was charged.

begin;

-- ===========================================================================
-- recurring_invoice
-- ===========================================================================
create table recurring_invoice (
    id                  uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- A customer MASTER record, not typed-in details: a subscription is a standing
    -- relationship, and an address that has to be re-typed every month is one that
    -- drifts. The customer's payment terms and language come from it at each run.
    customer_id         uuid not null references customer(id),

    name                text not null check (length(btrim(name)) > 0),

    -- Months between invoices. Not a free integer: these are the rhythms a
    -- business actually bills on (monthly, every two months, quarterly, every four
    -- months, half-yearly, yearly).
    interval_months     integer not null check (interval_months in (1, 2, 3, 4, 6, 12)),

    -- The first run date, and the anchor every later date is derived from.
    start_date          date not null,
    -- The last date a run may fall on, inclusive. Null: no end.
    end_date            date,
    -- Or a count. Null: no limit. Either ends the schedule, whichever comes first.
    max_runs            integer check (max_runs is null or max_runs >= 1),

    -- Payment term in days, overriding the customer's own. Null: use the customer's.
    due_days            integer check (due_days is null or due_days between 0 and 365),

    -- Annual price indexation, percent. numeric, never float (NFR-031).
    indexation_percent  numeric(6,3) not null default 0
                            check (indexation_percent >= 0 and indexation_percent <= 100),

    -- Whether a generated invoice is ISSUED (numbered and posted) or left as a draft
    -- for a person to check. Off by default: issuing is a claim to somebody outside
    -- the business, and a subscription that starts billing itself should be a
    -- decision, not a default. SENDING is never automatic either way.
    auto_issue          boolean not null default false,

    notes               text,

    status              text not null default 'active'
                            check (status in ('active', 'paused', 'ended')),

    -- How many invoices it has generated, and when the next is due. `next_run_on` is
    -- null once ended. Kept in step by the generator, in the same savepoint as the
    -- run row - see the header.
    runs_generated      integer not null default 0 check (runs_generated >= 0),
    next_run_on         date,

    -- Why the last attempt failed, if it did. A code, never prose (FR-UX-007): the
    -- sentence comes from the catalogue. Cleared by a successful run.
    last_error          text,
    last_error_at       timestamptz,

    created_by_user_id  uuid references users(id),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    constraint recurring_invoice_end_after_start check (
        end_date is null or end_date >= start_date
    ),
    -- An ended schedule has nothing next; anything else has a next date.
    constraint recurring_invoice_next_run check (
        (status = 'ended') = (next_run_on is null)
    )
);

create index recurring_invoice_administration_idx
    on recurring_invoice(administration_id, status);
create index recurring_invoice_organization_idx on recurring_invoice(organization_id);
-- The generator's query: active schedules that are due.
create index recurring_invoice_due_idx
    on recurring_invoice(administration_id, next_run_on)
    where status = 'active';
create index recurring_invoice_customer_idx on recurring_invoice(customer_id);

comment on table recurring_invoice is
    'FR-AR-008. A schedule that generates sales invoices on a rhythm. A template, '
    'not an invoice: generating one goes through InvoicingService like any other.';

-- ===========================================================================
-- recurring_invoice_line
-- ===========================================================================
create table recurring_invoice_line (
    id                    uuid primary key default gen_random_uuid(),
    organization_id       uuid not null references organization(id),
    administration_id     uuid not null references administration(id),
    recurring_invoice_id  uuid not null references recurring_invoice(id) on delete cascade,

    position              integer not null check (position >= 1),
    description           text not null check (length(btrim(description)) > 0),

    -- The same shape and precision as `sales_invoice_line` (0037), so a schedule's
    -- line becomes an invoice line without conversion.
    quantity              numeric(19,4) not null,
    -- The BASE price, before indexation.
    unit_price            numeric(19,4) not null,
    discount_percent      numeric(9,4) not null default 0
                              check (discount_percent >= 0 and discount_percent <= 100),
    vat_treatment         text not null references vat_treatment(code),

    constraint recurring_invoice_line_position_unique unique (recurring_invoice_id, position)
);

create index recurring_invoice_line_schedule_idx
    on recurring_invoice_line(recurring_invoice_id, position);
create index recurring_invoice_line_organization_idx on recurring_invoice_line(organization_id);

-- ===========================================================================
-- recurring_invoice_run
-- ===========================================================================
create table recurring_invoice_run (
    id                    uuid primary key default gen_random_uuid(),
    organization_id       uuid not null references organization(id),
    administration_id     uuid not null references administration(id),
    recurring_invoice_id  uuid not null references recurring_invoice(id),

    -- The date this run was SCHEDULED for, which is also the invoice date. Not the
    -- day it happened to be generated: catching up three missed months produces
    -- three invoices dated in the three months.
    run_date              date not null,
    invoice_id            uuid not null references sales_invoice(id),

    -- Whether this run issued the invoice, and if it was meant to and could not,
    -- why (a code). A schedule with auto_issue whose invoice is still a draft is
    -- one a person needs to look at, and this is what says so.
    issued                boolean not null default false,
    issue_error           text,

    created_at            timestamptz not null default now(),

    -- The idempotency key. See the header.
    constraint recurring_invoice_run_once_per_date unique (recurring_invoice_id, run_date),
    constraint recurring_invoice_run_error_means_draft check (
        issue_error is null or not issued
    )
);

create unique index recurring_invoice_run_invoice_idx on recurring_invoice_run(invoice_id);
create index recurring_invoice_run_administration_idx
    on recurring_invoice_run(administration_id, run_date desc);
create index recurring_invoice_run_organization_idx on recurring_invoice_run(organization_id);

comment on table recurring_invoice_run is
    'FR-AR-008. One row per invoice a schedule generated, keyed on (schedule, '
    'scheduled date) so a retry or a race cannot bill twice. Immutable apart from '
    'nothing: it is the record of what was generated.';

-- ===========================================================================
-- Tenant coherence
-- ===========================================================================
create or replace function recurring_invoice_guard() returns trigger as $$
declare
    v_org            uuid;
    v_customer_admin uuid;
begin
    select organization_id into v_org from administration where id = new.administration_id;
    if v_org is null then
        raise exception 'administration % does not exist', new.administration_id;
    end if;

    select administration_id into v_customer_admin from customer where id = new.customer_id;
    if v_customer_admin is distinct from new.administration_id then
        raise exception
            'a schedule bills a customer of its own administration: customer is in %, '
            'schedule claims % (CLAUDE.md rule 1)', v_customer_admin, new.administration_id;
    end if;

    new.organization_id := v_org;
    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger recurring_invoice_guard_trg
    before insert or update on recurring_invoice
    for each row execute function recurring_invoice_guard();

create or replace function recurring_invoice_child_guard() returns trigger as $$
declare
    v_admin uuid;
    v_org   uuid;
begin
    select administration_id, organization_id into v_admin, v_org
      from recurring_invoice where id = new.recurring_invoice_id;
    if v_admin is distinct from new.administration_id then
        raise exception
            'a schedule''s lines and runs belong to its administration: schedule is in %, '
            'row claims % (CLAUDE.md rule 1)', v_admin, new.administration_id;
    end if;
    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger recurring_invoice_line_guard_trg
    before insert or update on recurring_invoice_line
    for each row execute function recurring_invoice_child_guard();

-- A run's invoice must be the same administration's - a schedule that pointed at
-- another tenant's invoice would be a schedule reading it.
create or replace function recurring_invoice_run_guard() returns trigger as $$
declare
    v_admin         uuid;
    v_org           uuid;
    v_invoice_admin uuid;
begin
    -- The child guard's own checks, restated: a trigger function cannot be called
    -- from another function, only fired by a trigger.
    select administration_id, organization_id into v_admin, v_org
      from recurring_invoice where id = new.recurring_invoice_id;
    if v_admin is distinct from new.administration_id then
        raise exception
            'a schedule''s lines and runs belong to its administration: schedule is in %, '
            'row claims % (CLAUDE.md rule 1)', v_admin, new.administration_id;
    end if;
    new.organization_id := v_org;

    select administration_id into v_invoice_admin from sales_invoice where id = new.invoice_id;
    if v_invoice_admin is distinct from new.administration_id then
        raise exception
            'a run''s invoice belongs to the schedule''s administration: invoice is in %, '
            'run claims % (IAM-001)', v_invoice_admin, new.administration_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger recurring_invoice_run_guard_trg
    before insert on recurring_invoice_run
    for each row execute function recurring_invoice_run_guard();

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table recurring_invoice      owner to ledgr_migrator;
alter table recurring_invoice_line owner to ledgr_migrator;
alter table recurring_invoice_run  owner to ledgr_migrator;

-- A schedule is ENDED, never deleted: its runs point at invoices that exist, and
-- "what was this billing for" outlives the schedule. Lines are replaced wholesale
-- when a definition is edited, so they may be deleted; a run is a record of what was
-- generated and is neither updated nor deleted.
grant select, insert, update on recurring_invoice to ledgr_app;
grant select, insert, delete on recurring_invoice_line to ledgr_app;
grant select, insert on recurring_invoice_run to ledgr_app;
grant select on recurring_invoice, recurring_invoice_line, recurring_invoice_run to ledgr_ops;

do $$
declare
    t text;
begin
    foreach t in array array[
        'recurring_invoice', 'recurring_invoice_line', 'recurring_invoice_run'
    ] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format(
            'create policy %I on %I for select using (app.has_administration_access(administration_id))',
            t || '_select', t);
        execute format(
            'create policy %I on %I for insert with check (app.has_administration_access(administration_id))',
            t || '_insert', t);
    end loop;
end $$;

create policy recurring_invoice_update on recurring_invoice
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy recurring_invoice_line_delete on recurring_invoice_line
    for delete using (app.has_administration_access(administration_id));

commit;
