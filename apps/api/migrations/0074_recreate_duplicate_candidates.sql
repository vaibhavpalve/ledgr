-- 0074_recreate_duplicate_candidates.sql
-- expenses.duplicate_candidates was unreachable in production immediately after 0073 deployed -
-- the same class of gap ADR-100 found and fixed for app.ensure_posting_defaults. See
-- docs/decisions/ADR-100-recreate-missing-ensure-posting-defaults.md for the pattern this repeats,
-- and ADR-101 for this function's own history.
--
-- ===========================================================================
-- What actually happened is still not established - this fixes it regardless
-- ===========================================================================
--
-- 0073 dropped `expenses.duplicate_candidates(uuid, uuid, text, date, numeric, real)` (the
-- original 0036 signature) and created a new one with an added `p_invoice_number` parameter and
-- an added `invoice_number` output column. The deploy that ran 0073 succeeded with no error, yet
-- the very next call to the function failed in production:
--
--   asyncpg.exceptions.UndefinedFunctionError: function
--   expenses.duplicate_candidates(unknown, unknown, unknown, unknown, unknown, unknown, unknown)
--   does not exist
--
-- Two migrations in one session hitting the identical failure shape (0067's function, now 0036's)
-- points at something systemic in how this environment's function-defining migrations have
-- landed, not two unrelated one-off mistakes - worth investigating for its own sake, separately
-- from unblocking this feature now.
--
-- This migration does not guess why. It drops every signature this function could plausibly have
-- (both the pre-0073 shape and 0073's own, each IF EXISTS - a safe no-op for whichever was never
-- really there) and creates it fresh, identical in every column and every line of logic to 0073's
-- version. A tracked runner (ADR-064) WILL apply a migration under a filename it has not seen
-- before, which is what actually matters here, not diagnosing the drift.
--
-- Tenancy: no table/RLS changes. Ledger: not touched - this function only ever selects.
--
-- NFR-044: backward compatible, no downtime. Reversible:
--   drop function if exists expenses.duplicate_candidates(uuid, uuid, text, date, numeric, text, real);
--   -- then re-run 0036's original create or replace function expenses.duplicate_candidates(...)

begin;

drop function if exists expenses.duplicate_candidates(uuid, uuid, text, date, numeric, real);
drop function if exists expenses.duplicate_candidates(uuid, uuid, text, date, numeric, text, real);

create function expenses.duplicate_candidates(
    p_administration_id uuid,
    p_expense_id        uuid,
    p_supplier          text,
    p_on                date,
    p_gross_amount      numeric,
    p_invoice_number    text default null,
    p_similarity_floor  real default 0.45
)
returns table (
    expense_id      uuid,
    strength        text,
    supplier        text,
    expense_date    date,
    gross_amount    numeric,
    status          text,
    same_submitter  boolean,
    similarity      real,
    invoice_number  text
)
language sql
stable
as $$
    with subject as (
        select expenses.normalise_supplier(p_supplier) as supplier,
               p_on          as on_date,
               p_gross_amount as gross,
               (select submitted_by_user_id from expense where id = p_expense_id)
                   as submitter
        where p_supplier is not null
          and p_on is not null
          and p_gross_amount is not null
          and expenses.normalise_supplier(p_supplier) <> ''
    )
    select e.id,
           case
               when expenses.normalise_supplier(e.supplier) = s.supplier
                   then 'exact'
               else 'probable'
           end,
           e.supplier,
           e.expense_date,
           e.gross_amount,
           e.status,
           e.submitted_by_user_id is not distinct from s.submitter,
           case
               when expenses.normalise_supplier(e.supplier) = s.supplier then null
               else similarity(expenses.normalise_supplier(e.supplier), s.supplier)
           end,
           e.invoice_number
      from expense e, subject s
     where e.administration_id = p_administration_id
       and e.id is distinct from p_expense_id
       and e.expense_date = s.on_date
       and e.gross_amount = s.gross
       and e.supplier is not null
       and expenses.normalise_supplier(e.supplier) <> ''
       and (
           expenses.normalise_supplier(e.supplier) = s.supplier
           or similarity(expenses.normalise_supplier(e.supplier), s.supplier)
              >= p_similarity_floor
       )
     order by
        (expenses.normalise_supplier(e.supplier) = s.supplier) desc,
        similarity(expenses.normalise_supplier(e.supplier), s.supplier) desc nulls last,
        e.expense_date desc;
$$;

comment on function
    expenses.duplicate_candidates(uuid, uuid, text, date, numeric, text, real) is
    'FR-EXP-001g/ADR-101, recreated by ADR-100-style repair (this migration). Existing claims '
    'matching a supplier, date and amount, carrying their own invoice number for the caller to '
    'compare. Warns only - not SECURITY DEFINER, so RLS confines it to the caller''s own tenant.';

grant execute on function
    expenses.duplicate_candidates(uuid, uuid, text, date, numeric, text, real)
    to ledgr_app, ledgr_ops;

commit;
