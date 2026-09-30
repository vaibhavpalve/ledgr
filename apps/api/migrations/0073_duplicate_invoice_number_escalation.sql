-- 0073_duplicate_invoice_number_escalation.sql
-- FR-EXP-001g's duplicate check now also compares invoice numbers, to tell "the same
-- document, twice" apart from "two documents that happen to look alike". See
-- docs/decisions/ADR-101-invoice-number-escalates-duplicate-warnings.md.
--
-- ===========================================================================
-- Why this needed a new function, not create-or-replace
-- ===========================================================================
--
-- expenses.duplicate_candidates gains a new output column (invoice_number) and a new input
-- parameter (p_invoice_number). Postgres refuses `create or replace` when the return shape of a
-- `returns table (...)` function changes - the old one has to be dropped first.
--
-- ===========================================================================
-- What the new column means, and what does not change
-- ===========================================================================
--
-- The match rule itself (supplier normalised-equal-or-similar, date exact, amount exact) is
-- untouched - `strength` still means exactly what it always has. The new `invoice_number` column
-- is read-only data for the caller: api.expenses.duplicates.invoice_number_match (Python, pure)
-- decides "same"/"missing"/"different" from it, the same split api.expenses.duplicates.
-- is_exact_match already keeps for the supplier/date/amount rule - SQL supplies candidates and
-- their own facts, Python decides what a fact means.
--
-- Tenancy: no table or RLS policy changes - `expense.invoice_number` already exists and is
-- already scoped by this function's own administration_id filter. Ledger: not touched.
--
-- NFR-044: backward compatible (a function signature change behind one call site,
-- api.expenses.repository.duplicate_candidates, updated in the same commit), no downtime.
-- Reversible:
--   drop function if exists expenses.duplicate_candidates(uuid, uuid, text, date, numeric, text, real);
--   -- then re-run 0036's original create or replace function expenses.duplicate_candidates(...)

begin;

drop function if exists expenses.duplicate_candidates(uuid, uuid, text, date, numeric, real);

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
    -- An incomplete triple matches nothing. A claim somebody is still filling
    -- in is the normal state (FR-EXP-001c), not an error, so this returns no
    -- rows rather than refusing.
    with subject as (
        select expenses.normalise_supplier(p_supplier) as supplier,
               p_on          as on_date,
               p_gross_amount as gross,
               (select submitted_by_user_id from expense where id = p_expense_id)
                   as submitter
        where p_supplier is not null
          and p_on is not null
          and p_gross_amount is not null
          -- A supplier that normalises to NOTHING matches nothing. ':' and
          -- '...' both normalise to '', so without this every claim with a
          -- punctuation-only supplier would warn about every other one - and
          -- `length(btrim(':')) > 0` is true, so 0033's CHECK does not stop
          -- one being entered. A name carrying no letters or digits says
          -- nothing about which shop it was.
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
     -- The date and the amount are matched EXACTLY, on both strengths. The
     -- requirement names all three fields, and loosening two of them at once
     -- would turn a warning into noise - which is how a warning gets dismissed
     -- without being read.
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
     -- Exact first, then the closest of the rest: a person reads the top of
     -- this list and stops.
     order by
        (expenses.normalise_supplier(e.supplier) = s.supplier) desc,
        similarity(expenses.normalise_supplier(e.supplier), s.supplier) desc nulls last,
        e.expense_date desc;
$$;

comment on function
    expenses.duplicate_candidates(uuid, uuid, text, date, numeric, text, real) is
    'FR-EXP-001g/ADR-101. Existing claims matching a supplier, date and amount, carrying their own '
    'invoice number for the caller to compare - api.expenses.duplicates.invoice_number_match '
    'decides what a match, a mismatch or a missing number means. Warns only - there is '
    'deliberately no constraint, because most duplicates are ordinary. Not SECURITY DEFINER, so '
    'RLS confines it to the caller''s own tenant.';

grant execute on function
    expenses.duplicate_candidates(uuid, uuid, text, date, numeric, text, real)
    to ledgr_app, ledgr_ops;

commit;
