-- 0033_expense_form.sql
-- FR-EXP-001b, FR-EXP-001e (PRD §6.7). See
-- docs/decisions/ADR-032-expense-form.md.
--
--   FR-EXP-001b  The expense form asks for the minimum - date, supplier, gross
--                amount, VAT rate, category - with VAT and net calculated
--                automatically and the category defaulting from the user's
--                history. Everything else is optional and hidden by default.
--   FR-EXP-001e  Payment method is captured at entry - paid by business
--                account, business card, or personally (reimbursable) -
--                because it determines the posting and cannot be reliably
--                inferred later.
--
-- ===========================================================================
-- "VAT rate" is a TREATMENT, not a number somebody types
-- ===========================================================================
--
-- 0032 gave `expense` a bare `vat_rate`. That was wrong, and this migration
-- corrects it while the table is still empty of anything but drafts.
--
-- This system already models VAT as an effective-dated TREATMENT (0028,
-- ADR-027): `btw_21`, `btw_9`, `btw_0`, `btw_verlegd` and the rest, each with
-- rates carrying a `valid_from`. The shipped ruleset makes the point on its
-- own - `btw_21` is 19.00 from 2001-01-01 and 21.00 from 2012-10-01. A receipt
-- dated 2012-09-30 is a 19% receipt and one dated 2012-10-01 is a 21% one, and
-- no amount of care at the keyboard makes a typed rate track that.
--
-- So the form captures a treatment, and `vat.rate_on(treatment, expense_date)`
-- supplies the rate. `vat_rate` is still STORED, as the rate that was resolved
-- at entry: CMP-014's "historical periods keep the rules that applied at the
-- time" has to survive a later ruleset load, and a value recomputed on read
-- would not.
--
-- ===========================================================================
-- One figure is rounded; the other is subtracted
-- ===========================================================================
--
--     vat_amount = round(gross x rate / (100 + rate))
--     net_amount = gross - vat            <- a GENERATED column
--
-- Rounding both halves independently can leave `net + vat` a cent away from
-- `gross`, and that cent is a ledger that does not balance (FR-GL-001). Making
-- net a generated column removes the possibility entirely: it is not a value
-- any writer supplies, so it cannot disagree.
--
-- VAT is the rounded half rather than net because the VAT figure is what gets
-- filed (FR-VAT-001's rubrieken). The rounded number should be the one on the
-- return; the residue belongs in net, where nobody files it.
--
-- The CHECK below holds `vat_amount` to exactly what the rule gives, for every
-- writer - not only for the service that normally computes it.

begin;

-- ===========================================================================
-- The split, as a function anyone can call
-- ===========================================================================
-- IMMUTABLE, which is what lets the CHECK constraint below use it. Pure
-- arithmetic: no tenant, no reads, no clock.
--
-- Postgres `round(numeric, 2)` rounds half AWAY FROM ZERO, which is what a
-- Dutch tax figure does and what anybody checking the sum by hand does. It
-- agrees with api.expenses.vat's explicit ROUND_HALF_UP - and neither
-- inherits a default, because Python's decimal default is ROUND_HALF_EVEN and
-- would round 0.025 to 0.02.
create or replace function expenses.vat_from_gross(
    p_gross numeric,
    p_rate  numeric
)
returns numeric
language sql
immutable
as $$
    select case
        when p_gross is null or p_rate is null then null
        -- Not an optimisation: it keeps every zero-rated treatment
        -- (btw_0, btw_vrijgesteld, btw_verlegd, btw_icp, btw_export) off the
        -- division entirely.
        when p_rate = 0 then 0.00::numeric
        else round(p_gross * p_rate / (100 + p_rate), 2)
    end;
$$;

comment on function expenses.vat_from_gross(numeric, numeric) is
    'FR-EXP-001b. The VAT contained in a gross amount at a percentage rate. '
    'Mirrored by api.expenses.vat.vat_from_gross; tests/expenses/vat_cases.py '
    'runs one table against both.';

-- ===========================================================================
-- expense gains its computed figures and its treatment
-- ===========================================================================
alter table expense
    add column vat_treatment text references vat_treatment(code),
    add column vat_amount    numeric(19, 2) check (vat_amount is null or vat_amount >= 0);

-- FR-EXP-001b's "net calculated automatically", as a column no writer supplies.
alter table expense
    add column net_amount numeric(19, 2)
        generated always as (gross_amount - vat_amount) stored;

comment on column expense.vat_treatment is
    'FR-EXP-001b. WHICH VAT applies (0028''s effective-dated treatments), not '
    'a typed rate: btw_21 was 19% before 2012-10-01 and 21% after.';
comment on column expense.vat_rate is
    'CMP-014. The rate vat.rate_on() gave for this expense''s DATE, stored so '
    'a later ruleset load cannot restate a historical claim.';
comment on column expense.net_amount is
    'FR-EXP-001b. Generated: gross - vat. Never supplied, so net + vat = gross '
    'is an identity rather than something that usually holds (FR-GL-001).';

-- The stored VAT is exactly what the rule gives. Not "close to" - exactly, for
-- every writer including ledgr_ops and a superuser at a psql prompt.
alter table expense
    add constraint expense_vat_is_derived check (
        vat_amount is null
        or vat_amount = expenses.vat_from_gross(gross_amount, vat_rate)
    );

-- ===========================================================================
-- `btw_marge` cannot be entered on this form, and that is not an omission
-- ===========================================================================
-- The margin scheme charges VAT on the MARGIN, not on the sale price - the
-- shipped ruleset says so on the row itself. Extracting VAT from a gross
-- receipt under btw_marge would compute it on the whole amount and overstate
-- it by whatever the purchase cost was, which on a resold item is most of it.
--
-- Refused here rather than left to the service, because the wrong number would
-- be arithmetically consistent with everything else in the row: gross, rate
-- and vat_amount would all agree, and only the SCHEME would be wrong. Nothing
-- downstream would catch it.
alter table expense
    add constraint expense_no_margin_scheme check (
        vat_treatment is distinct from 'btw_marge'
    );

-- ===========================================================================
-- FR-EXP-001b + FR-EXP-001e: what `ready` means
-- ===========================================================================
-- 0032 let an expense reach `ready` with every field null, because the form
-- did not exist yet. It does now, and `ready` means the minimum has been
-- given: an expense with no amount and no payment method cannot be approved
-- (FR-EXP-002) or reimbursed (FR-EXP-003), so releasing one as ready is
-- releasing a claim nobody can act on.
--
-- Payment method is in this list because FR-EXP-001e says it "determines the
-- posting and cannot be reliably inferred later". A claim that reached
-- approval without it would have to be sent back to somebody's memory.
alter table expense
    add constraint expense_ready_is_complete check (
        status <> 'ready'
        or (
            expense_date is not null
            and supplier is not null and length(btrim(supplier)) > 0
            and gross_amount is not null
            and vat_treatment is not null
            and vat_rate is not null
            and vat_amount is not null
            and category is not null and length(btrim(category)) > 0
            and payment_method is not null
        )
    );

-- A treatment without the rate it resolved to, or the reverse, is half an
-- answer: the pair is what CMP-014 preserves.
alter table expense
    add constraint expense_treatment_and_rate_together check (
        (vat_treatment is null) = (vat_rate is null)
    );

create index expense_vat_treatment_idx on expense(administration_id, vat_treatment);
-- The category-suggestion lookup below, and FR-EXP-001b's "defaulting from the
-- user's history": most recent for this supplier, then most frequent overall.
create index expense_category_history_idx
    on expense(administration_id, submitted_by_user_id, supplier, created_at desc)
    where category is not null;

-- ===========================================================================
-- FR-EXP-001b: "the category defaulting from the user's history"
-- ===========================================================================
-- Two questions in order, because they answer different things:
--
--   1. What did THIS PERSON last file THIS SUPPLIER under? A lunch at the same
--      cafe is the same category every time, and this is the answer that is
--      right often enough to accept without thinking.
--   2. Failing that, what do they file most things under? A weaker guess, and
--      still better than an empty field on a form somebody fills in twenty
--      times a week.
--
-- The user's history, not the administration's: FR-EXP-001b says "the user's",
-- and §8.4 scopes an Expense Submitter to their own submissions. A default
-- drawn from a colleague's habits would also leak what they have been
-- claiming.
--
-- Not SECURITY DEFINER, deliberately. It reads `expense`, which carries RLS,
-- so it sees exactly what the calling request may see and a suggestion cannot
-- cross a tenant boundary.
create or replace function expenses.suggest_category(
    p_administration_id uuid,
    p_user_id           uuid,
    p_supplier          text default null
)
returns text
language sql
stable
as $$
    with for_supplier as (
        select e.category
          from expense e
         where e.administration_id = p_administration_id
           and e.submitted_by_user_id = p_user_id
           and e.category is not null
           and p_supplier is not null
           -- Case- and space-insensitive: "Albert Heijn" and "albert heijn "
           -- are one supplier to a person typing them.
           and lower(btrim(e.supplier)) = lower(btrim(p_supplier))
         order by e.created_at desc
         limit 1
    ),
    most_used as (
        select e.category
          from expense e
         where e.administration_id = p_administration_id
           and e.submitted_by_user_id = p_user_id
           and e.category is not null
         group by e.category
         -- Ties broken by the most recent use, so the answer is stable rather
         -- than whichever row the planner happened to reach first.
         order by count(*) desc, max(e.created_at) desc
         limit 1
    )
    select coalesce(
        (select category from for_supplier),
        (select category from most_used)
    );
$$;

comment on function expenses.suggest_category(uuid, uuid, text) is
    'FR-EXP-001b. The category to default to: what this user last filed this '
    'supplier under, else what they file most things under. A suggestion - '
    'always editable (FR-EXP-001c).';

-- ===========================================================================
-- Grants
-- ===========================================================================
grant execute on function expenses.vat_from_gross(numeric, numeric)
    to ledgr_app, ledgr_ops;
grant execute on function expenses.suggest_category(uuid, uuid, text)
    to ledgr_app, ledgr_ops;

-- The treatment reference table is public law, not tenant data - 0028 grants
-- SELECT on it already; the FK above needs nothing further.

commit;
