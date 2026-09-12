-- 0036_duplicate_detection.sql
-- FR-EXP-001g (PRD §6.7). See docs/decisions/ADR-034-duplicate-detection.md.
--
--   FR-EXP-001g  Duplicate detection WARNS when a receipt matching an existing
--                expense on supplier, date and amount is captured.
--
-- ===========================================================================
-- Warn, never block - which is a statement about this file too
-- ===========================================================================
--
-- Nothing here is a constraint. There is no unique index on (supplier, date,
-- amount) and there is not going to be one: legitimate duplicates exist and
-- are ordinary - two identical coffees on one morning, two colleagues each
-- buying the same train ticket, a subscription billed twice because the first
-- attempt failed. A constraint would be wrong about the money and would teach
-- people to work around it, which is worse than the duplicates it stopped.
--
-- So this migration adds a QUERY and an index to make it fast. The decision
-- stays with the person.
--
-- ===========================================================================
-- Two strengths, and why only one of them lives in Python too
-- ===========================================================================
--
--   exact     the normalised suppliers are equal, and so are the date and the
--             gross. `api.expenses.duplicates.is_exact_match` decides the same
--             thing with no database, and duplicate_cases.py runs one table
--             against both.
--   probable  the suppliers are SIMILAR - "ALBERT HEIJN 1043" beside "Albert
--             Heijn". pg_trgm's job alone: reimplementing its similarity in
--             Python to the same answer is a promise that breaks the first
--             time Postgres tunes it.
--
-- ===========================================================================
-- Administration-wide, not per submitter
-- ===========================================================================
--
-- The case worth catching most is two people claiming one receipt - a shared
-- lunch, one bill, two photographs. Scoping the search to the submitter's own
-- claims would miss exactly that.
--
-- What comes back is deliberately narrow: the matching claim's id, its own
-- triple (which tells the reader nothing they did not just type), its status,
-- and whether the submitter was the same person. NOT who the other person is.
-- "This receipt has already been claimed" is what somebody needs in order to
-- act; a colleague's name is not, and a duplicate check is a poor place to
-- learn what one's colleagues have been spending.
--
-- Note for whoever builds row-level scoping: api.authz.matrix's note on
-- EXTENSION_CAPABILITIES says §8.4 scopes an Expense Submitter to their own
-- submissions and that the scoping does not exist yet. When it does, this
-- function keeps working - it is not SECURITY DEFINER, so RLS confines it -
-- but the `same_submitter` flag becomes the only thing an Expense Submitter
-- can see about somebody else's claim, and that is worth re-reading then.

begin;

-- ===========================================================================
-- The normalisation, shared with Python
-- ===========================================================================
-- Three steps in this order: collapse internal whitespace, strip punctuation
-- at either end, casefold. `api.expenses.duplicates.normalise_supplier` does
-- the same three in the same order.
--
-- Punctuation is stripped only at the EDGES. "Jan's Cafe" and "Jans Cafe" are
-- arguably one shop, and deciding so by removing the apostrophe is the kind of
-- normalisation that eventually merges two real suppliers whose names differ
-- by a hyphen.
--
-- IMMUTABLE so the expression index below can use it.
create or replace function expenses.normalise_supplier(p_supplier text)
returns text
language sql
immutable
as $$
    select case
        when p_supplier is null then null
        else lower(
            regexp_replace(
                regexp_replace(btrim(p_supplier), '\s+', ' ', 'g'),
                '^[^[:alnum:]]+|[^[:alnum:]]+$', '', 'g'
            )
        )
    end;
$$;

comment on function expenses.normalise_supplier(text) is
    'FR-EXP-001g. The form a supplier name is compared in. Mirrored by '
    'api.expenses.duplicates.normalise_supplier; tests/expenses/'
    'duplicate_cases.py runs one table against both.';

-- ---------------------------------------------------------------------------
-- Indexes the search needs
-- ---------------------------------------------------------------------------
-- The exact half: an administration's claims by normalised supplier, date and
-- amount. Covers the common lookup in one index scan.
create index expense_duplicate_exact_idx
    on expense(
        administration_id,
        expenses.normalise_supplier(supplier),
        expense_date,
        gross_amount
    )
    where supplier is not null
      and expense_date is not null
      and gross_amount is not null;

-- The similar half. Trigram over the normalised name, so "ALBERT HEIJN 1043"
-- is reachable from "Albert Heijn" without scanning the administration.
create index expense_duplicate_similar_idx
    on expense using gin (expenses.normalise_supplier(supplier) gin_trgm_ops)
    where supplier is not null;

-- ===========================================================================
-- The search
-- ===========================================================================
create or replace function expenses.duplicate_candidates(
    p_administration_id uuid,
    p_expense_id        uuid,
    p_supplier          text,
    p_on                date,
    p_gross_amount      numeric,
    p_similarity_floor  real default 0.45
)
returns table (
    expense_id     uuid,
    strength       text,
    supplier       text,
    expense_date   date,
    gross_amount   numeric,
    status         text,
    same_submitter boolean,
    similarity     real
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
           end
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

comment on function expenses.duplicate_candidates(uuid, uuid, text, date, numeric, real) is
    'FR-EXP-001g. Existing claims matching a supplier, date and amount. Warns '
    'only - there is deliberately no constraint, because legitimate duplicates '
    'are ordinary. Not SECURITY DEFINER, so RLS confines it to the caller''s '
    'own tenant.';

grant execute on function expenses.normalise_supplier(text) to ledgr_app, ledgr_ops;
grant execute on function expenses.duplicate_candidates(uuid, uuid, text, date, numeric, real)
    to ledgr_app, ledgr_ops;

commit;
