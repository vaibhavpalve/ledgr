-- 0052_sales_invoice_payment.sql
-- FR-AR-010 / FR-AR-012 / FR-AR-009 groundwork (PRD §6.3): the receivable has an
-- outstanding balance. See docs/decisions/ADR-070-sales-invoice-payments.md.
--
-- ===========================================================================
-- Why this exists before dunning does
-- ===========================================================================
--
-- SI-04 (the reminder ladder) asks "which invoices are overdue?", and overdue
-- means "issued, past its due date, and NOT PAID". Until now nothing in the
-- schema could say an invoice was paid: 0037 stops at issue, 0040 posts the
-- receivable, and bank reconciliation (FR-BNK-*) is not built. A reminder ladder
-- over that would chase customers who had already paid, which is worse than no
-- ladder. So the receivable gets its other half: a recorded payment, posted
-- through the ledger like everything else.
--
-- ===========================================================================
-- A payment is a fact about an invoice AND an entry in the books
-- ===========================================================================
--
--     Dr  Bank / Kas (an asset account)    amount
--     Cr  Debiteuren (AR control)          amount   <- carries the sub-ledger party
--
-- The entry is written by `ledger.post_entry` through `LedgerService`, never by
-- this table's owner (CLAUDE.md rule 1), and `journal_entry_id` is NOT NULL: a
-- payment row with no entry would be a receipt the books do not know about,
-- which is the mirror of the gap 0040 closed for issue.
--
-- The debtor's balance therefore falls by the same amount in the sub-ledger
-- (`ledger.subledger_balance`) and in `outstanding` below, and there is no
-- second balance to drift.
--
-- ===========================================================================
-- Immutable, with one exception: it can be voided, once
-- ===========================================================================
--
-- A mistaken payment (wrong invoice, wrong amount, a bounced cheque) is
-- corrected the way the ledger corrects everything - by a REVERSING entry
-- (FR-GL-003), never by editing or deleting. The payment row records that:
-- `voided_at`, `voided_by_user_id` and `void_journal_entry_id` are set together,
-- once, and nothing else about the row may change. There is no DELETE grant.
-- A voided payment stops counting toward `paid`; the invoice is owed again.
--
-- ===========================================================================
-- What an invoice OWES
-- ===========================================================================
--
--     outstanding = gross  -  credited  -  paid
--
--   gross     the frozen VAT totals (0037) of the issued invoice
--   credited  the gross of its credit note, if it has one (a credit note credits
--             the whole invoice - AlreadyCredited, 0037)
--   paid      the sum of its payments that are not voided
--
-- Never negative: `sales_invoice_payment_guard` refuses the payment that would
-- take it below zero, under a row lock on the invoice so two payments recorded at
-- the same moment cannot both fit. Overpayment and split allocation are
-- FR-BNK-005, which arrives with bank reconciliation; refusing is safer than
-- silently parking somebody's money.

begin;

-- ===========================================================================
-- sales_invoice_payment
-- ===========================================================================
create table sales_invoice_payment (
    id                  uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1. Both, on every row; RLS reads administration_id.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    invoice_id          uuid not null references sales_invoice(id),

    -- numeric, never float (NFR-031); two decimals like every money column here.
    amount              numeric(14,2) not null check (amount > 0),
    paid_on             date not null,

    -- How the money arrived. Chooses the journal (cash -> the cash journal,
    -- everything else -> the bank journal); it does not change the accounting.
    method              text not null check (method in (
                            'bank_transfer', 'cash', 'card', 'other'
                        )),

    -- What the customer quoted, or what the bank statement said. Free text and
    -- optional: the invoice reference is the usual value.
    reference           text check (reference is null or length(btrim(reference)) > 0),

    -- The asset account the money landed in. Picked per payment rather than
    -- mapped once, because an administration commonly has more than one bank
    -- account and only the person recording the payment knows which.
    bank_account_id     uuid not null references ledger_account(id),

    -- The receipt, in the books. NOT NULL - see the header.
    journal_entry_id    uuid not null references journal_entry(id),

    recorded_by_user_id uuid not null references users(id),
    recorded_at         timestamptz not null default now(),

    -- A void, set together and once.
    voided_at           timestamptz,
    voided_by_user_id   uuid references users(id),
    void_journal_entry_id uuid references journal_entry(id),

    constraint sales_invoice_payment_void_is_complete check (
        (voided_at is null) = (voided_by_user_id is null)
        and (voided_at is null) = (void_journal_entry_id is null)
    )
);

-- One receipt entry per payment: the entry IS the payment's evidence, and two
-- payments sharing one would make voiding either of them reverse both.
create unique index sales_invoice_payment_entry_idx
    on sales_invoice_payment(journal_entry_id);

create index sales_invoice_payment_invoice_idx
    on sales_invoice_payment(invoice_id, paid_on);
create index sales_invoice_payment_administration_idx
    on sales_invoice_payment(administration_id, paid_on desc);
create index sales_invoice_payment_organization_idx
    on sales_invoice_payment(organization_id);

comment on table sales_invoice_payment is
    'FR-AR-010. A payment received against one issued invoice, with the ledger '
    'entry that recorded it. Immutable except for a single void, which is itself '
    'a reversing entry - never an edit or a delete.';

-- ---------------------------------------------------------------------------
-- What may be paid, and into what
-- ---------------------------------------------------------------------------
-- A trigger rather than CHECKs because every condition lives on another table.
create or replace function sales_invoice_payment_guard() returns trigger as $$
declare
    v_status      text;
    v_admin       uuid;
    v_org         uuid;
    v_credits     uuid;
    v_acct_admin  uuid;
    v_acct_type   text;
    v_acct_ctrl   text;
    v_outstanding numeric;
begin
    -- FOR UPDATE: serialises payments against one invoice, so the outstanding
    -- balance read below cannot be spent twice by two concurrent inserts.
    select status, administration_id, organization_id, credits_invoice_id
      into v_status, v_admin, v_org, v_credits
      from sales_invoice where id = new.invoice_id
       for update;

    if not found then
        raise exception 'sales invoice % does not exist', new.invoice_id;
    end if;

    -- Tenant coherence: a payment naming a different administration from its
    -- invoice would settle one tenant's receivable from another tenant's books.
    if v_admin is distinct from new.administration_id then
        raise exception
            'payment belongs to administration %, but its invoice belongs to % '
            '(IAM-001)', new.administration_id, v_admin;
    end if;

    -- A draft owes nothing; a credit note is not a debt. Only an issued invoice
    -- that is not itself a credit note can be paid.
    if v_status <> 'issued' then
        raise exception
            'invoice % is a % and cannot be paid; only an issued invoice is owed '
            '(FR-AR-004)', new.invoice_id, v_status;
    end if;
    if v_credits is not null then
        raise exception
            'invoice % is a credit note and cannot be paid (FR-AR-001)', new.invoice_id;
    end if;

    -- The account the money landed in must be this administration's, a plain
    -- asset (bank / cash), and NOT the receivable itself - debiting Debiteuren
    -- against Debiteuren would move nothing and settle nothing.
    select administration_id, account_type, control_kind
      into v_acct_admin, v_acct_type, v_acct_ctrl
      from ledger_account where id = new.bank_account_id;

    if v_acct_admin is distinct from new.administration_id then
        raise exception
            'a payment lands in the administration''s own account: account is in %, '
            'payment claims % (CLAUDE.md rule 1)', v_acct_admin, new.administration_id;
    end if;
    if v_acct_type is distinct from 'asset' or v_acct_ctrl is not null then
        raise exception
            'a payment is received into a plain asset account (bank or cash); '
            'this is a % account%', v_acct_type,
            case when v_acct_ctrl is not null
                 then ' that is a control account (' || v_acct_ctrl || ')' else '' end;
    end if;

    -- A payment never takes the balance below zero. Read through the one
    -- definition of "outstanding", under the lock taken above; each statement in
    -- a plpgsql trigger takes a fresh snapshot, so a payment another transaction
    -- committed while this one waited for the lock is seen.
    select b.outstanding into v_outstanding
      from invoicing.invoice_balances(new.administration_id, new.invoice_id) b;

    if new.amount > coalesce(v_outstanding, 0) then
        raise exception
            'a payment of % exceeds the % still outstanding on invoice %; '
            'overpayment and split allocation arrive with bank reconciliation '
            '(FR-BNK-005)', new.amount, coalesce(v_outstanding, 0), new.invoice_id;
    end if;

    -- Derived rather than trusted, so the two can never disagree.
    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_payment_guard_trg
    before insert on sales_invoice_payment
    for each row execute function sales_invoice_payment_guard();

-- ---------------------------------------------------------------------------
-- Nothing changes but the single void
-- ---------------------------------------------------------------------------
create or replace function sales_invoice_payment_immutable() returns trigger as $$
begin
    if old.voided_at is not null then
        raise exception
            'payment % was voided at %; a voided payment is history and cannot '
            'change (FR-GL-003)', old.id, old.voided_at;
    end if;

    if new.id                  is distinct from old.id
       or new.organization_id  is distinct from old.organization_id
       or new.administration_id is distinct from old.administration_id
       or new.invoice_id       is distinct from old.invoice_id
       or new.amount           is distinct from old.amount
       or new.paid_on          is distinct from old.paid_on
       or new.method           is distinct from old.method
       or new.reference        is distinct from old.reference
       or new.bank_account_id  is distinct from old.bank_account_id
       or new.journal_entry_id is distinct from old.journal_entry_id
       or new.recorded_by_user_id is distinct from old.recorded_by_user_id
       or new.recorded_at      is distinct from old.recorded_at
    then
        raise exception
            'payment % is a record of money received; only voiding it is allowed. '
            'Correct a mistake by voiding it and recording the right one (FR-GL-003).',
            old.id;
    end if;

    if new.voided_at is null then
        raise exception 'payment % update changes nothing; only a void is allowed', old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_payment_immutable_trg
    before update on sales_invoice_payment
    for each row execute function sales_invoice_payment_immutable();

-- ===========================================================================
-- What an invoice owes
-- ===========================================================================
-- One row per ISSUED, non-credit-note invoice of the administration, with the
-- three figures and their difference. A function rather than a view so it takes
-- the administration explicitly and cannot be read without naming one - the
-- shape `invoicing.undelivered_invoices` (0041) already has.
--
-- `gross` is the frozen VAT totals (0037), which are what the document and the
-- ledger both say. `credited` is the credit note's gross negated (its totals are
-- negative, ADR-037), counted only once that credit note is itself issued.
create or replace function invoicing.invoice_balances(
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
    outstanding       numeric
)
language sql
stable
as $$
    select b.id, b.invoice_reference, b.invoice_date, b.due_date,
           b.customer_id, b.customer_name,
           b.gross, b.credited, b.paid,
           b.gross - b.credited - b.paid
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
                            and p.voided_at is null), 0)                        as paid
          from sales_invoice si
         where si.administration_id = p_administration_id
           and (p_invoice_id is null or si.id = p_invoice_id)
           and si.status = 'issued'
           and si.credits_invoice_id is null
      ) b;
$$;

comment on function invoicing.invoice_balances(uuid, uuid) is
    'FR-AR-010. What each issued invoice of one administration owes (or just one, '
    'when p_invoice_id is given): gross, less its credit note, less its unvoided '
    'payments. The ONE definition of "outstanding" - the overpayment guard, '
    'dunning and the aged-receivables report all read it.';

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table sales_invoice_payment owner to ledgr_migrator;

-- No DELETE: a payment is the record that money arrived, and "we never got
-- that" is what this table exists to answer. Corrected by a void, never removed.
grant select, insert, update on sales_invoice_payment to ledgr_app;
grant select on sales_invoice_payment to ledgr_ops;

grant execute on function invoicing.invoice_balances(uuid, uuid) to ledgr_app, ledgr_ops;

alter table sales_invoice_payment enable row level security;
alter table sales_invoice_payment force row level security;

create policy sales_invoice_payment_select on sales_invoice_payment
    for select using (app.has_administration_access(administration_id));
create policy sales_invoice_payment_insert on sales_invoice_payment
    for insert with check (app.has_administration_access(administration_id));
create policy sales_invoice_payment_update on sales_invoice_payment
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;
