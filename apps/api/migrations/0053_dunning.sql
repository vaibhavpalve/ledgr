-- 0053_dunning.sql
-- FR-AR-010 (PRD §6.3), SI-04. See docs/decisions/ADR-071-dunning-ladder.md.
--
--   FR-AR-010  Dunning: configurable reminder ladder with escalation, statutory
--              interest and collection cost calculation, and pause-per-customer.
--
-- Builds on 0052: an invoice is overdue when it is issued, past its due date and
-- has an OUTSTANDING balance (`invoicing.invoice_balances`). Nothing here defines
-- "owed" a second time.
--
-- ===========================================================================
-- Four tables, and what each is for
-- ===========================================================================
--
--   dunning_step             the administration's ladder: which reminder is due
--                            how many days after the due date, and what it may
--                            claim. NO ROWS means "use the built-in default
--                            ladder", which is different from a ladder of zero
--                            steps ("this administration does not chase") -
--                            `dunning_ladder_configured` records that choice.
--   dunning_pause            a customer whose invoices are not chased. Presence
--                            of a row IS the pause; removing it un-pauses.
--   dunning_reminder         every reminder actually SENT, with the amounts it
--                            claimed. Immutable: it is the record of what a
--                            customer was told they owed.
--   statutory_interest_rate  the government-published rates, effective-dated,
--                            append-only, and EMPTY until an operator loads them.
--
-- ===========================================================================
-- The interest rate table ships empty, on purpose
-- ===========================================================================
--
-- The statutory rates change every half year (the commercial one) and are set by
-- the government. A rate written into a migration would be wrong within months and
-- would be a demand for money on customers' letters. So there is no seed: until an
-- operator loads rates, a step that charges interest is REFUSED (see
-- `api.invoicing.dunning.Blocker.INTEREST_RATE_MISSING`), never sent with a guess.
-- Loading is `insert ... into statutory_interest_rate` as `ledgr_ops`, which is
-- the only role with the grant, in the way 0028 restricts tax-rule writes.

begin;

-- ===========================================================================
-- dunning_ladder_configured / dunning_step
-- ===========================================================================
-- One row per administration that has CHOSEN a ladder (including an empty one).
-- Without this, "no rows in dunning_step" could not tell "never configured, use
-- the default" from "configured to chase nobody".
create table dunning_ladder_configured (
    administration_id   uuid primary key references administration(id),
    organization_id     uuid not null references organization(id),
    configured_by_user_id uuid references users(id),
    configured_at       timestamptz not null default now()
);

create table dunning_step (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    position            integer not null check (position between 1 and 6),
    -- Days AFTER the due date on which this step becomes due.
    days_after_due      integer not null check (days_after_due >= 1),
    kind                text not null check (kind in ('friendly', 'reminder', 'formal_notice')),
    charge_interest     boolean not null default false,
    charge_collection_cost boolean not null default false,

    -- Collection cost is a consequence of a formal notice and nothing else - the
    -- same rule `dunning.validate_ladder` enforces, restated here so no writer
    -- can sidestep it.
    constraint dunning_step_cost_needs_a_formal_notice check (
        not charge_collection_cost or kind = 'formal_notice'
    ),
    constraint dunning_step_position_unique unique (administration_id, position),
    constraint dunning_step_days_unique unique (administration_id, days_after_due)
);

create index dunning_step_organization_idx on dunning_step(organization_id);

comment on table dunning_step is
    'FR-AR-010. One rung of an administration''s reminder ladder. See '
    'dunning_ladder_configured for why "no rows" and "an empty ladder" differ.';

-- ===========================================================================
-- dunning_pause
-- ===========================================================================
create table dunning_pause (
    customer_id         uuid primary key references customer(id),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    paused_by_user_id   uuid not null references users(id),
    paused_at           timestamptz not null default now(),
    -- Why: "in dispute", "payment plan agreed". Free text, optional.
    reason              text check (reason is null or length(btrim(reason)) > 0)
);

create index dunning_pause_administration_idx on dunning_pause(administration_id);
create index dunning_pause_organization_idx on dunning_pause(organization_id);

comment on table dunning_pause is
    'FR-AR-010. A customer whose invoices are not chased. The row is the pause; '
    'deleting it resumes the ladder where it stood.';

-- ===========================================================================
-- dunning_reminder
-- ===========================================================================
create table dunning_reminder (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    invoice_id          uuid not null references sales_invoice(id),
    step_position       integer not null check (step_position between 1 and 6),
    kind                text not null check (kind in ('friendly', 'reminder', 'formal_notice')),

    -- The dispatch that carried it. NOT NULL: a reminder row exists only for one
    -- that was ACCEPTED by the provider - a queued or failed dispatch is not a
    -- reminder sent, and nothing drains the queue (0041), so recording it as one
    -- would stop the step ever being retried.
    delivery_id         uuid not null references invoice_delivery(id),

    -- What the customer was told, snapshotted: the outstanding balance and, where
    -- the step claimed them, the interest and the collection cost. numeric, never
    -- float (NFR-031).
    outstanding_amount  numeric(14,2) not null check (outstanding_amount > 0),
    interest_amount     numeric(14,2) check (interest_amount is null or interest_amount >= 0),
    collection_cost_amount numeric(14,2)
                            check (collection_cost_amount is null or collection_cost_amount >= 0),
    -- For a formal notice: the last day the customer was given to pay.
    pay_by              date,
    days_overdue        integer not null check (days_overdue >= 1),

    sent_by_user_id     uuid not null references users(id),
    sent_at             timestamptz not null default now(),

    -- One reminder per step per invoice, ever. The database is what stops two
    -- people (or one person twice) sending the same step to the same customer.
    constraint dunning_reminder_once_per_step unique (invoice_id, step_position),
    constraint dunning_reminder_costs_only_on_a_notice check (
        collection_cost_amount is null or kind = 'formal_notice'
    ),
    constraint dunning_reminder_notice_has_a_deadline check (
        (kind = 'formal_notice') = (pay_by is not null)
    )
);

create index dunning_reminder_administration_idx
    on dunning_reminder(administration_id, sent_at desc);
create index dunning_reminder_organization_idx on dunning_reminder(organization_id);

comment on table dunning_reminder is
    'FR-AR-010. A reminder that was sent, with the amounts it claimed. Immutable '
    '- the record of what a customer was told they owed.';

-- ===========================================================================
-- statutory_interest_rate - global reference data, append-only, ships EMPTY
-- ===========================================================================
create table statutory_interest_rate (
    kind        text not null check (kind in ('commercial', 'consumer')),
    valid_from  date not null,
    -- A percentage per year, e.g. 10.150. numeric, never float (NFR-031).
    rate        numeric(6,3) not null check (rate >= 0 and rate <= 100),
    -- Where it came from: the publication and date. Required - a rate with no
    -- source is a rate nobody can check.
    source_note text not null check (length(btrim(source_note)) > 0),
    loaded_at   timestamptz not null default now(),
    primary key (kind, valid_from)
);

comment on table statutory_interest_rate is
    'FR-AR-010. Government-published statutory interest rates, effective-dated '
    'and append-only. Ships EMPTY: a rate in a migration would be wrong within '
    'months. Loaded by ledgr_ops.';

-- Append-only, the way vat_rate is (0028): a rate that changes is a NEW row with
-- a later valid_from, never an edit - a past reminder's interest must stay
-- explicable by the rate that was in force.
create or replace function statutory_interest_rate_immutable() returns trigger as $$
begin
    raise exception
        'statutory interest rates are append-only: add a row with a later valid_from '
        'instead of changing or removing one (CMP-014)';
end;
$$ language plpgsql;

create trigger statutory_interest_rate_no_update_trg
    before update on statutory_interest_rate
    for each row execute function statutory_interest_rate_immutable();
create trigger statutory_interest_rate_no_delete_trg
    before delete on statutory_interest_rate
    for each row execute function statutory_interest_rate_immutable();

-- ===========================================================================
-- Tenant coherence
-- ===========================================================================
-- Each tenant table's organization is DERIVED from its administration, never
-- trusted from the caller, and anything it points at must be in the same
-- administration (CLAUDE.md rule 1).
create or replace function dunning_derive_organization() returns trigger as $$
declare
    v_org uuid;
begin
    select organization_id into v_org from administration where id = new.administration_id;
    if v_org is null then
        raise exception 'administration % does not exist', new.administration_id;
    end if;
    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger dunning_step_org_trg before insert or update on dunning_step
    for each row execute function dunning_derive_organization();
create trigger dunning_ladder_configured_org_trg
    before insert or update on dunning_ladder_configured
    for each row execute function dunning_derive_organization();

create or replace function dunning_pause_same_tenant() returns trigger as $$
declare
    v_admin uuid;
begin
    select administration_id into v_admin from customer where id = new.customer_id;
    if v_admin is distinct from new.administration_id then
        raise exception
            'a pause belongs to the customer''s own administration: customer is in %, '
            'pause claims % (CLAUDE.md rule 1)', v_admin, new.administration_id;
    end if;
    select organization_id into new.organization_id
      from administration where id = new.administration_id;
    return new;
end;
$$ language plpgsql;

create trigger dunning_pause_same_tenant_trg before insert on dunning_pause
    for each row execute function dunning_pause_same_tenant();

create or replace function dunning_reminder_guard() returns trigger as $$
declare
    v_admin   uuid;
    v_org     uuid;
    v_status  text;
    v_credits uuid;
begin
    select administration_id, organization_id, status, credits_invoice_id
      into v_admin, v_org, v_status, v_credits
      from sales_invoice where id = new.invoice_id;

    if not found then
        raise exception 'sales invoice % does not exist', new.invoice_id;
    end if;
    if v_admin is distinct from new.administration_id then
        raise exception
            'reminder belongs to administration %, but its invoice belongs to % (IAM-001)',
            new.administration_id, v_admin;
    end if;
    if v_status <> 'issued' or v_credits is not null then
        raise exception
            'invoice % is not an issued invoice that is owed money; only one is chased '
            '(FR-AR-010)', new.invoice_id;
    end if;
    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger dunning_reminder_guard_trg before insert on dunning_reminder
    for each row execute function dunning_reminder_guard();

-- ===========================================================================
-- What is overdue, once
-- ===========================================================================
-- Every issued, non-credit-note invoice of one administration that is past its
-- due date and still owes money, with everything the dunning rules need to decide
-- what to do about it: whether the customer is a business (which chooses the
-- interest rate kind), whether they are paused, and which steps have gone out.
--
-- `is_business`: a VAT number on the invoice, or a KvK number on the customer
-- master. There is no consumer flag on `customer` (0039), and that is a JUDGEMENT
-- the assessment surfaces rather than hides - a consumer invoiced with a VAT
-- number typed in would be treated as a business.
create or replace function invoicing.overdue_invoices(p_administration_id uuid, p_today date)
returns table (
    invoice_id        uuid,
    invoice_reference text,
    invoice_date      date,
    due_date          date,
    customer_id       uuid,
    customer_name     text,
    outstanding       numeric,
    is_business       boolean,
    is_paused         boolean,
    sent_positions    integer[]
)
language sql
stable
as $$
    select b.invoice_id, b.invoice_reference, b.invoice_date, b.due_date,
           b.customer_id, b.customer_name, b.outstanding,
           (si.customer_vat_number is not null
            or exists (select 1 from customer c
                        where c.id = b.customer_id and c.kvk_number is not null)),
           exists (select 1 from dunning_pause p where p.customer_id = b.customer_id),
           coalesce(array(select r.step_position from dunning_reminder r
                           where r.invoice_id = b.invoice_id order by r.step_position),
                    '{}'::integer[])
      from invoicing.invoice_balances(p_administration_id) b
      join sales_invoice si on si.id = b.invoice_id
     where b.due_date is not null
       and b.due_date < p_today
       and b.outstanding > 0
     order by b.due_date, b.invoice_reference;
$$;

comment on function invoicing.overdue_invoices(uuid, date) is
    'FR-AR-010. Issued invoices past their due date that still owe money, with '
    'the customer''s pause state and the steps already sent. The one definition of '
    '"overdue" - the overview and the bulk chase (SI-11) both read it.';

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table dunning_ladder_configured owner to ledgr_migrator;
alter table dunning_step              owner to ledgr_migrator;
alter table dunning_pause             owner to ledgr_migrator;
alter table dunning_reminder          owner to ledgr_migrator;
alter table statutory_interest_rate   owner to ledgr_migrator;

-- The ladder is rewritten as a whole, so it needs DELETE. A pause is removed to
-- resume. A REMINDER is neither updated nor deleted: it is what a customer was
-- told, and "we never sent that" is exactly the question it answers.
grant select, insert, update, delete on dunning_ladder_configured to ledgr_app;
grant select, insert, update, delete on dunning_step to ledgr_app;
grant select, insert, delete on dunning_pause to ledgr_app;
grant select, insert on dunning_reminder to ledgr_app;
grant select on dunning_ladder_configured, dunning_step, dunning_pause,
                dunning_reminder to ledgr_ops;

-- Rates are global reference data: everyone reads, only operations writes.
revoke all on statutory_interest_rate from public;
grant select on statutory_interest_rate to ledgr_app, ledgr_ops;
grant insert on statutory_interest_rate to ledgr_ops;

grant execute on function invoicing.overdue_invoices(uuid, date) to ledgr_app, ledgr_ops;

do $$
declare
    t text;
begin
    foreach t in array array[
        'dunning_ladder_configured', 'dunning_step', 'dunning_pause', 'dunning_reminder'
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

create policy dunning_ladder_configured_update on dunning_ladder_configured
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy dunning_ladder_configured_delete on dunning_ladder_configured
    for delete using (app.has_administration_access(administration_id));
create policy dunning_step_update on dunning_step
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy dunning_step_delete on dunning_step
    for delete using (app.has_administration_access(administration_id));
create policy dunning_pause_delete on dunning_pause
    for delete using (app.has_administration_access(administration_id));

commit;
