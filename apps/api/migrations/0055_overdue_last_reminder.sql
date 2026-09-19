-- 0055_overdue_last_reminder.sql
-- SI-11 / FR-AR-010. See docs/decisions/ADR-073-chase-all-overdue.md.
--
-- `invoicing.overdue_invoices` (0053) now also reports WHEN the last reminder about
-- each invoice went out, so the rules can space one reminder from the next.
--
-- ===========================================================================
-- Why this exists
-- ===========================================================================
--
-- The ladder's steps are spaced from the DUE date (`dunning_step.days_after_due`),
-- not from each other. So an invoice 40 days overdue that nobody had chased would
-- get step 1, and sending again would immediately send step 2, then the formal
-- notice: every step's day had already passed. One button press to send the next
-- step (SI-04) allowed it; a bulk "chase everything" (SI-11) retried once would do it
-- to every customer at once. `api.invoicing.dunning.MIN_DAYS_BETWEEN_REMINDERS`
-- refuses it, and this column is the fact that rule needs.
--
-- ===========================================================================
-- Compatible by construction
-- ===========================================================================
--
-- A function's result columns cannot change under `create or replace`, so this drops
-- and recreates it in one transaction. The new function returns every column the old
-- one did, in the same order, with `last_sent_on` appended - so code that reads the
-- old columns by name keeps working through a deploy (NFR-044).

begin;

drop function if exists invoicing.overdue_invoices(uuid, date);

create function invoicing.overdue_invoices(p_administration_id uuid, p_today date)
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
    sent_positions    integer[],
    -- The day the most recent reminder about this invoice was sent, or null if none.
    last_sent_on      date
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
                    '{}'::integer[]),
           (select max(r.sent_at)::date from dunning_reminder r
             where r.invoice_id = b.invoice_id)
      from invoicing.invoice_balances(p_administration_id) b
      join sales_invoice si on si.id = b.invoice_id
     where b.due_date is not null
       and b.due_date < p_today
       and b.outstanding > 0
     order by b.due_date, b.invoice_reference;
$$;

comment on function invoicing.overdue_invoices(uuid, date) is
    'FR-AR-010. Issued invoices past their due date that still owe money, with '
    'the customer''s pause state, the steps already sent and the day of the last '
    'reminder. The one definition of "overdue" - the overview and the bulk chase '
    '(SI-11) both read it.';

grant execute on function invoicing.overdue_invoices(uuid, date) to ledgr_app, ledgr_ops;

commit;
