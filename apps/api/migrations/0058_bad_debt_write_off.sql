-- 0058_bad_debt_write_off.sql
-- FR-AR-013 (PRD 6.3), SI-10. See docs/decisions/ADR-076-bad-debt-write-off.md.
--
--   FR-AR-013  Bad debt write-off with the associated VAT reclaim entry.
--
-- ===========================================================================
-- What a write-off is
-- ===========================================================================
--
-- An uncollectable invoice stops being an asset. The books say so with an entry, and the
-- receivable's outstanding balance falls to zero WITHOUT anybody having paid:
--
--     Dr  Afschrijving oninbare vorderingen (an expense account)    outstanding
--     Cr  Debiteuren (AR control)                                   outstanding  <- party
--
-- The entry is written by `ledger.post_entry` through `LedgerService`, never by this table's
-- owner (CLAUDE.md rule 1); `journal_entry_id` is NOT NULL, the mirror of 0052's argument.
--
-- ===========================================================================
-- The VAT reclaim is a SECOND entry, and may come later
-- ===========================================================================
--
-- The VAT charged on an uncollectable invoice can be claimed back (Wet OB art. 29), but not
-- at once: only after a waiting period from the due date, or earlier if the customer is
-- insolvent. So the reclaim is not part of the write-off; it is its own entry, and the row
-- records it when it happens:
--
--     Dr  Te betalen omzetbelasting (per VAT treatment)    the VAT part of the write-off
--     Cr  Afschrijving oninbare vorderingen                the same
--
-- The VAT part is proportional to the unpaid share of each VAT group of the invoice, worked
-- out once at write-off and stored in `vat_split` so the later reclaim posts exactly what
-- was decided, not a recomputation.
--
-- ===========================================================================
-- Immutable, with two one-way steps
-- ===========================================================================
--
-- A row changes twice at most: the VAT reclaim is recorded (once), and the write-off is
-- voided (once) - which is itself reversing entries (FR-GL-003), never an edit or delete. A
-- customer who pays after all is handled by voiding the write-off, which owes the invoice
-- again; the VAT comes back through the same reversal. There is no DELETE grant.
--
-- ===========================================================================
-- What an invoice owes, now
-- ===========================================================================
--
--     outstanding = gross - credited - paid - written_off
--
-- `invoicing.invoice_balances` (0052) is the ONE definition, so the overpayment guard,
-- dunning and the ageing report all stop chasing a written-off invoice by reading it. Its
-- result gains a `written_off` column, appended after the existing ones so code that reads
-- them by name keeps working through a deploy (NFR-044).

begin;

-- ===========================================================================
-- sales_invoice_write_off
-- ===========================================================================
create table sales_invoice_write_off (
    id                  uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1. Both, on every row; RLS reads administration_id.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    invoice_id          uuid not null references sales_invoice(id),

    -- numeric, never float (NFR-031). The WHOLE outstanding balance: a partial write-off
    -- would leave a receivable nobody intends to collect on the books.
    amount              numeric(14,2) not null check (amount > 0),
    -- The VAT-bearing part of `amount` (proportional), reclaimable in due course.
    vat_amount          numeric(14,2) not null check (vat_amount >= 0 and vat_amount <= amount),
    -- [{"treatment": "btw_21", "vat": "210.00"}, ...]; sums to vat_amount.
    vat_split           jsonb not null default '[]'::jsonb,

    written_off_on      date not null,
    reason              text not null check (length(btrim(reason)) > 0),
    customer_insolvent  boolean not null default false,

    expense_account_id  uuid not null references ledger_account(id),
    journal_entry_id    uuid not null references journal_entry(id),

    recorded_by_user_id uuid not null references users(id),
    recorded_at         timestamptz not null default now(),

    -- The VAT reclaim, set together and once.
    vat_reclaimed_on            date,
    vat_reclaim_journal_entry_id uuid references journal_entry(id),
    vat_reclaimed_by_user_id    uuid references users(id),

    -- A void, set together and once. Reverses the write-off entry, and the reclaim entry
    -- too when there was one.
    voided_at                   timestamptz,
    voided_by_user_id           uuid references users(id),
    void_journal_entry_id       uuid references journal_entry(id),
    void_reclaim_journal_entry_id uuid references journal_entry(id),

    constraint sales_invoice_write_off_reclaim_is_complete check (
        (vat_reclaimed_on is null) = (vat_reclaim_journal_entry_id is null)
        and (vat_reclaimed_on is null) = (vat_reclaimed_by_user_id is null)
    ),
    constraint sales_invoice_write_off_void_is_complete check (
        (voided_at is null) = (voided_by_user_id is null)
        and (voided_at is null) = (void_journal_entry_id is null)
        and (void_reclaim_journal_entry_id is null or voided_at is not null)
    ),
    -- Nothing to reclaim means no reclaim entry.
    constraint sales_invoice_write_off_reclaim_needs_vat check (
        vat_reclaim_journal_entry_id is null or vat_amount > 0
    )
);

-- One ACTIVE write-off per invoice. A voided one is history and does not count, so an
-- invoice can be written off again after a void.
create unique index sales_invoice_write_off_active_idx
    on sales_invoice_write_off(invoice_id) where voided_at is null;
create unique index sales_invoice_write_off_entry_idx
    on sales_invoice_write_off(journal_entry_id);
create index sales_invoice_write_off_administration_idx
    on sales_invoice_write_off(administration_id, written_off_on desc);
create index sales_invoice_write_off_organization_idx
    on sales_invoice_write_off(organization_id);

comment on table sales_invoice_write_off is
    'FR-AR-013. An issued invoice written off as uncollectable, with the ledger entry '
    'that recorded it and, later, the VAT reclaim entry. Immutable except for recording '
    'the reclaim once and voiding once - each a reversing/adding entry, never an edit.';

-- ---------------------------------------------------------------------------
-- What may be written off, and into what
-- ---------------------------------------------------------------------------
create or replace function sales_invoice_write_off_guard() returns trigger as $$
declare
    v_status      text;
    v_admin       uuid;
    v_org         uuid;
    v_credits     uuid;
    v_acct_admin  uuid;
    v_acct_type   text;
    v_outstanding numeric;
begin
    -- FOR UPDATE: serialises write-offs and payments against one invoice, so the balance
    -- read below cannot be spent twice.
    select status, administration_id, organization_id, credits_invoice_id
      into v_status, v_admin, v_org, v_credits
      from sales_invoice where id = new.invoice_id
       for update;

    if not found then
        raise exception 'sales invoice % does not exist', new.invoice_id;
    end if;
    if v_admin is distinct from new.administration_id then
        raise exception
            'write-off belongs to administration %, but its invoice belongs to % (IAM-001)',
            new.administration_id, v_admin;
    end if;
    if v_status <> 'issued' or v_credits is not null then
        raise exception
            'invoice % is not an issued invoice that is owed money and cannot be written off '
            '(FR-AR-004)', new.invoice_id;
    end if;

    select administration_id, account_type
      into v_acct_admin, v_acct_type
      from ledger_account where id = new.expense_account_id;
    if v_acct_admin is distinct from new.administration_id then
        raise exception
            'a write-off lands in the administration''s own account: account is in %, '
            'write-off claims % (CLAUDE.md rule 1)', v_acct_admin, new.administration_id;
    end if;
    if v_acct_type is distinct from 'expense' then
        raise exception
            'a write-off is booked to an expense account; this is a % account', v_acct_type;
    end if;

    select b.outstanding into v_outstanding
      from invoicing.invoice_balances(new.administration_id, new.invoice_id) b;

    if new.amount is distinct from coalesce(v_outstanding, 0) then
        raise exception
            'a write-off of % is not the % still outstanding on invoice %; the whole '
            'outstanding balance is written off, never part of it (FR-AR-013)',
            new.amount, coalesce(v_outstanding, 0), new.invoice_id;
    end if;

    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_write_off_guard_trg
    before insert on sales_invoice_write_off
    for each row execute function sales_invoice_write_off_guard();

-- ---------------------------------------------------------------------------
-- Nothing changes but the reclaim (once) and the void (once)
-- ---------------------------------------------------------------------------
create or replace function sales_invoice_write_off_immutable() returns trigger as $$
begin
    if old.voided_at is not null then
        raise exception
            'write-off % was voided at %; a voided write-off is history and cannot change '
            '(FR-GL-003)', old.id, old.voided_at;
    end if;

    if new.id                  is distinct from old.id
       or new.organization_id  is distinct from old.organization_id
       or new.administration_id is distinct from old.administration_id
       or new.invoice_id       is distinct from old.invoice_id
       or new.amount           is distinct from old.amount
       or new.vat_amount       is distinct from old.vat_amount
       or new.vat_split        is distinct from old.vat_split
       or new.written_off_on   is distinct from old.written_off_on
       or new.reason           is distinct from old.reason
       or new.customer_insolvent is distinct from old.customer_insolvent
       or new.expense_account_id is distinct from old.expense_account_id
       or new.journal_entry_id is distinct from old.journal_entry_id
       or new.recorded_by_user_id is distinct from old.recorded_by_user_id
       or new.recorded_at      is distinct from old.recorded_at
    then
        raise exception
            'write-off % is a record of a decision; only recording its VAT reclaim or '
            'voiding it is allowed (FR-GL-003).', old.id;
    end if;

    -- The reclaim is recorded once: it cannot be replaced or cleared.
    if old.vat_reclaim_journal_entry_id is not null
       and (new.vat_reclaimed_on is distinct from old.vat_reclaimed_on
            or new.vat_reclaim_journal_entry_id is distinct from old.vat_reclaim_journal_entry_id
            or new.vat_reclaimed_by_user_id is distinct from old.vat_reclaimed_by_user_id)
    then
        raise exception 'write-off % already reclaimed its VAT; that cannot be changed', old.id;
    end if;

    if new.voided_at is null
       and new.vat_reclaim_journal_entry_id is not distinct from old.vat_reclaim_journal_entry_id
    then
        raise exception 'write-off % update changes nothing', old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_write_off_immutable_trg
    before update on sales_invoice_write_off
    for each row execute function sales_invoice_write_off_immutable();

-- ===========================================================================
-- invoicing.invoice_balances - now net of write-offs
-- ===========================================================================
-- A function's result columns cannot change under `create or replace`, so this drops and
-- recreates it in one transaction; `written_off` is appended, every earlier column is
-- unchanged and in the same order.
drop function if exists invoicing.invoice_balances(uuid, uuid);

create function invoicing.invoice_balances(
    p_administration_id uuid,
    p_invoice_id        uuid default null
)
returns table (
    invoice_id        uuid,
    invoice_reference text,
    invoice_date      date,
    due_date          date,
    customer_id       uuid,
    customer_name     text,
    gross             numeric,
    credited          numeric,
    paid              numeric,
    outstanding       numeric,
    written_off       numeric
)
language sql
stable
as $$
    select b.id, b.invoice_reference, b.invoice_date, b.due_date,
           b.customer_id, b.customer_name,
           b.gross, b.credited, b.paid,
           b.gross - b.credited - b.paid - b.written_off,
           b.written_off
      from (
        select si.id, si.invoice_reference, si.invoice_date, si.due_date,
               si.customer_id, si.customer_name,
               coalesce((select sum(t.taxable_amount + t.vat_amount)
                           from sales_invoice_vat_total t
                          where t.invoice_id = si.id), 0)                      as gross,
               coalesce((select -sum(t.taxable_amount + t.vat_amount)
                           from sales_invoice c
                           join sales_invoice_vat_total t on t.invoice_id = c.id
                          where c.credits_invoice_id = si.id
                            and c.status = 'issued'), 0)                        as credited,
               coalesce((select sum(p.amount)
                           from sales_invoice_payment p
                          where p.invoice_id = si.id
                            and p.voided_at is null), 0)                        as paid,
               coalesce((select sum(w.amount)
                           from sales_invoice_write_off w
                          where w.invoice_id = si.id
                            and w.voided_at is null), 0)                        as written_off
          from sales_invoice si
         where si.administration_id = p_administration_id
           and (p_invoice_id is null or si.id = p_invoice_id)
           and si.status = 'issued'
           and si.credits_invoice_id is null
      ) b;
$$;

comment on function invoicing.invoice_balances(uuid, uuid) is
    'FR-AR-010 / FR-AR-013. What each issued invoice of one administration owes (or just '
    'one, when p_invoice_id is given): gross, less its credit note, less its unvoided '
    'payments, less an unvoided write-off. The ONE definition of "outstanding" - the '
    'overpayment guard, dunning and the aged-receivables report all read it.';

grant execute on function invoicing.invoice_balances(uuid, uuid) to ledgr_app, ledgr_ops;

-- ===========================================================================
-- invoicing.receivable_items - a write-off counts from its own date (0054)
-- ===========================================================================
create or replace function invoicing.receivable_items(
    p_administration_id uuid,
    p_as_of             date
)
returns table (
    invoice_id        uuid,
    invoice_reference text,
    invoice_date      date,
    due_date          date,
    customer_id       uuid,
    customer_name     text,
    gross             numeric,
    credited          numeric,
    paid              numeric,
    outstanding       numeric
)
language sql
stable
as $$
    select b.id, b.invoice_reference, b.invoice_date, b.due_date,
           b.customer_id, b.customer_name,
           b.gross, b.credited, b.paid,
           b.gross - b.credited - b.paid - b.written_off
      from (
        select si.id, si.invoice_reference, si.invoice_date, si.due_date,
               si.customer_id, si.customer_name,
               coalesce((select sum(t.taxable_amount + t.vat_amount)
                           from sales_invoice_vat_total t
                          where t.invoice_id = si.id), 0)                       as gross,
               coalesce((select -sum(t.taxable_amount + t.vat_amount)
                           from sales_invoice c
                           join sales_invoice_vat_total t on t.invoice_id = c.id
                          where c.credits_invoice_id = si.id
                            and c.status = 'issued'
                            and c.invoice_date <= p_as_of), 0)                   as credited,
               coalesce((select sum(p.amount)
                           from sales_invoice_payment p
                          where p.invoice_id = si.id
                            and p.paid_on <= p_as_of
                            and (p.voided_at is null
                                 or p.voided_at::date > p_as_of)), 0)            as paid,
               -- A write-off voided LATER was still a write-off on that date.
               coalesce((select sum(w.amount)
                           from sales_invoice_write_off w
                          where w.invoice_id = si.id
                            and w.written_off_on <= p_as_of
                            and (w.voided_at is null
                                 or w.voided_at::date > p_as_of)), 0)            as written_off
          from sales_invoice si
         where si.administration_id = p_administration_id
           and si.status = 'issued'
           and si.credits_invoice_id is null
           and si.invoice_date <= p_as_of
      ) b
     where b.gross - b.credited - b.paid - b.written_off > 0
     order by b.due_date nulls last, b.invoice_reference;
$$;

-- ===========================================================================
-- invoicing.customer_movements - a write-off is a credit on the account (0054)
-- ===========================================================================
-- It credits the debtor in the ledger, so the statement must show it or the closing balance
-- stops equalling the debtor's sub-ledger balance.
create or replace function invoicing.customer_movements(
    p_administration_id uuid,
    p_customer_id       uuid
)
returns table (
    movement_date     date,
    kind              text,
    reference         text,
    invoice_id        uuid,
    debit             numeric,
    credit            numeric
)
language sql
stable
as $$
    select m.movement_date, m.kind, m.reference, m.invoice_id, m.debit, m.credit
      from (
        select si.invoice_date as movement_date, 'invoice'::text as kind,
               si.invoice_reference as reference, si.id as invoice_id,
               coalesce((select sum(t.taxable_amount + t.vat_amount)
                           from sales_invoice_vat_total t where t.invoice_id = si.id), 0) as debit,
               0::numeric as credit,
               1 as sort_order
          from sales_invoice si
         where si.administration_id = p_administration_id
           and si.customer_id = p_customer_id
           and si.status = 'issued'
           and si.credits_invoice_id is null

        union all
        select cn.invoice_date, 'credit_note', cn.invoice_reference, cn.credits_invoice_id,
               0::numeric,
               coalesce((select -sum(t.taxable_amount + t.vat_amount)
                           from sales_invoice_vat_total t where t.invoice_id = cn.id), 0),
               2
          from sales_invoice cn
         where cn.administration_id = p_administration_id
           and cn.customer_id = p_customer_id
           and cn.status = 'issued'
           and cn.credits_invoice_id is not null

        union all
        select p.paid_on, 'payment', si.invoice_reference, p.invoice_id,
               0::numeric, p.amount, 3
          from sales_invoice_payment p
          join sales_invoice si on si.id = p.invoice_id
         where p.administration_id = p_administration_id
           and si.customer_id = p_customer_id

        union all
        select p.voided_at::date, 'payment_void', si.invoice_reference, p.invoice_id,
               p.amount, 0::numeric, 4
          from sales_invoice_payment p
          join sales_invoice si on si.id = p.invoice_id
         where p.administration_id = p_administration_id
           and si.customer_id = p_customer_id
           and p.voided_at is not null

        union all
        -- A write-off lowers it, on the day it was written off.
        select w.written_off_on, 'write_off', si.invoice_reference, w.invoice_id,
               0::numeric, w.amount, 5
          from sales_invoice_write_off w
          join sales_invoice si on si.id = w.invoice_id
         where w.administration_id = p_administration_id
           and si.customer_id = p_customer_id

        union all
        -- A voided write-off raises it again, on the day it was voided.
        select w.voided_at::date, 'write_off_void', si.invoice_reference, w.invoice_id,
               w.amount, 0::numeric, 6
          from sales_invoice_write_off w
          join sales_invoice si on si.id = w.invoice_id
         where w.administration_id = p_administration_id
           and si.customer_id = p_customer_id
           and w.voided_at is not null
      ) m
     order by m.movement_date, m.sort_order, m.reference;
$$;

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table sales_invoice_write_off owner to ledgr_migrator;

-- No DELETE: a write-off is a decision somebody must be able to account for.
grant select, insert, update on sales_invoice_write_off to ledgr_app;
grant select on sales_invoice_write_off to ledgr_ops;

alter table sales_invoice_write_off enable row level security;
alter table sales_invoice_write_off force row level security;

create policy sales_invoice_write_off_select on sales_invoice_write_off
    for select using (app.has_administration_access(administration_id));
create policy sales_invoice_write_off_insert on sales_invoice_write_off
    for insert with check (app.has_administration_access(administration_id));
create policy sales_invoice_write_off_update on sales_invoice_write_off
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;
