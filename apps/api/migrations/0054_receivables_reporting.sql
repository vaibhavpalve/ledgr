-- 0054_receivables_reporting.sql
-- FR-AR-012 / FR-RPT-001 (PRD §6.3, §6.9), SI-06. See
-- docs/decisions/ADR-072-aged-receivables-and-statements.md.
--
--   FR-AR-012  Aged receivables reporting and per-customer statement of account.
--
-- Read-only: two functions and no tables. Everything here is derived from what
-- 0037 (invoices), 0052 (payments) and the credit-note link already record, so
-- there is nothing new to keep in step and nothing new to secure beyond what those
-- tables already carry (RLS on administration_id, read under the caller's own
-- session - the functions are SECURITY INVOKER, so a tenant sees only its rows).
--
-- ===========================================================================
-- "Outstanding as of a date" is NOT "outstanding now"
-- ===========================================================================
--
-- `invoicing.invoice_balances` (0052) answers "what is owed NOW", counting every
-- unvoided payment, and it is what the overpayment guard reads - so it errs toward
-- counting money as received. An AGEING report is asked for a past date ("as at
-- the end of the quarter") and must answer what the books said THEN, or the same
-- report run next month would silently rewrite last month's. So
-- `invoicing.receivable_items` filters every movement by its own date:
--
--   invoice      counted if invoice_date <= as_of
--   credit note  counted if ITS invoice_date <= as_of
--   payment      counted if paid_on <= as_of AND it was not voided on or before
--                as_of (a payment voided LATER was still a payment on that date)
--
-- The two agree whenever as_of is on or after every payment's date, which a test
-- asserts. They differ only for a payment dated after the report date, which is
-- exactly the case where they should.
--
-- ===========================================================================
-- A statement is one list of movements
-- ===========================================================================
--
-- `invoicing.customer_movements` returns every movement on one customer's account,
-- oldest first; the opening balance, the running balance and the closing balance
-- are all arithmetic over that one list (in `api.invoicing.receivables`). There is
-- no separate "balance brought forward" query to disagree with the lines:
--
--   invoice        debit   gross
--   credit note    credit  its gross, positive
--   payment        credit  amount, on paid_on
--   payment void   debit   amount, on the day it was voided
--
-- These are the same four movements the ledger records against the debtor's party
-- (0040, 0052), so a customer's closing balance equals `ledger.subledger_balance`
-- for their party - the reconciliation the integration test asserts.

begin;

-- ===========================================================================
-- invoicing.receivable_items - the open items of one administration, as of a date
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
           b.gross - b.credited - b.paid
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
                                 or p.voided_at::date > p_as_of)), 0)            as paid
          from sales_invoice si
         where si.administration_id = p_administration_id
           and si.status = 'issued'
           and si.credits_invoice_id is null
           and si.invoice_date <= p_as_of
      ) b
     where b.gross - b.credited - b.paid > 0
     order by b.due_date nulls last, b.invoice_reference;
$$;

comment on function invoicing.receivable_items(uuid, date) is
    'FR-AR-012. The invoices of one administration that still owed money on '
    'p_as_of, with what was owed then: every movement filtered by its own date, so '
    'a report for a past date does not change when it is run again later. Differs '
    'from invoicing.invoice_balances only for a payment dated after p_as_of.';

-- ===========================================================================
-- invoicing.customer_movements - one customer's account, every movement
-- ===========================================================================
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
        -- An invoice raises the debt.
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
        -- A credit note lowers it. Its totals are negative (ADR-037), so the
        -- credit is the absolute value, keeping both columns unsigned as the
        -- ledger's own are.
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
        -- A payment lowers it, on the day the money arrived.
        select p.paid_on, 'payment', si.invoice_reference, p.invoice_id,
               0::numeric, p.amount, 3
          from sales_invoice_payment p
          join sales_invoice si on si.id = p.invoice_id
         where p.administration_id = p_administration_id
           and si.customer_id = p_customer_id

        union all
        -- A voided payment raises it again, on the day it was voided.
        select p.voided_at::date, 'payment_void', si.invoice_reference, p.invoice_id,
               p.amount, 0::numeric, 4
          from sales_invoice_payment p
          join sales_invoice si on si.id = p.invoice_id
         where p.administration_id = p_administration_id
           and si.customer_id = p_customer_id
           and p.voided_at is not null
      ) m
     -- A day's invoices before its credits before its payments: the order a
     -- person reading the statement expects, and stable for the running balance.
     order by m.movement_date, m.sort_order, m.reference;
$$;

comment on function invoicing.customer_movements(uuid, uuid) is
    'FR-AR-012. Every movement on one customer''s account - invoices, credit '
    'notes, payments and voided payments - oldest first. The statement''s opening, '
    'running and closing balances are all arithmetic over this one list, and it '
    'equals the debtor''s ledger sub-ledger by construction (0040, 0052).';

-- ===========================================================================
-- Grants
-- ===========================================================================
-- SECURITY INVOKER (the default): the tables' own RLS applies to the caller, so a
-- tenant reads only its own rows through either function.
grant execute on function invoicing.receivable_items(uuid, date) to ledgr_app, ledgr_ops;
grant execute on function invoicing.customer_movements(uuid, uuid) to ledgr_app, ledgr_ops;

commit;
