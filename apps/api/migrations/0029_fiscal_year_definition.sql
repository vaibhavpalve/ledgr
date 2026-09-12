-- 0029_fiscal_year_definition.sql
-- FR-ONB-006 (PRD §6.1). See docs/decisions/ADR-028-fiscal-year-definition.md.
--
--   FR-ONB-006  Fiscal year definition, including non-calendar and short first
--               years.
--
-- 0001 created `fiscal_year` and `period` and left both to be filled in by
-- hand. Every fixture in the test suite inserts them one INSERT at a time,
-- which is fine for a fixture and is not a feature: an administration being
-- onboarded needs a year defined and its periods derived, correctly, for a
-- year that may start in July and may be nine months long.
--
-- ===========================================================================
-- How periods are derived
-- ===========================================================================
--
-- Walk from the fiscal year's start date, taking whole calendar blocks -
-- months or quarters - and truncating the first and last to the year's actual
-- bounds:
--
--   cursor = start
--   while cursor <= end:
--       emit(cursor .. least(end of cursor's calendar block, end))
--       cursor = that end + 1 day
--
-- Which gives, for the cases FR-ONB-006 names:
--
--   calendar year, monthly      12 whole months
--   July-June year, monthly     12 whole months, Jul .. Jun
--   short first year            2026-03-15 .. 2026-12-31 monthly:
--                               a 17-day stub, then nine whole months
--   long first year             handled by the same walk; NL permits a first
--                               book year of up to about 24 months
--
-- ===========================================================================
-- Why blocks are CALENDAR-aligned, even for a non-calendar year
-- ===========================================================================
--
-- The tempting alternative for quarterly is fiscal quarters counted from the
-- year's own start - a May year giving May-Jul, Aug-Oct, Nov-Jan, Feb-Apr.
-- Exactly four periods, and wrong here.
--
-- `period.status` includes 'vat_filed' (0021) and CMP-014 keys a filing's rule
-- fingerprint on the period's end date, so in this schema a period is also the
-- unit a VAT return is filed for. Dutch VAT returns are filed for CALENDAR
-- months and CALENDAR quarters, whatever the fiscal year does. A May-July
-- period straddles two calendar quarters and cannot be filed as either.
--
-- So every period this derives lies inside exactly one calendar month
-- (monthly) or one calendar quarter (quarterly), and a VAT return for a
-- calendar period is a whole number of periods. The visible cost is that a
-- quarterly year starting in May has five periods, not four - two of them
-- stubs. That is the fiscal year genuinely straddling calendar quarters, and
-- showing it is better than hiding it behind a period that cannot be filed.
--
-- ===========================================================================
-- A one-day period is now legal, deliberately
-- ===========================================================================
--
-- 0001 wrote `check (end_date > start_date)`. A fiscal year beginning on the
-- last day of a month - 2026-03-31, say - derives a first period of
-- 2026-03-31 .. 2026-03-31, which that constraint rejects.
--
-- The alternative was to absorb a one-day stub into the following period, and
-- that is worse: the merged period would run 2026-03-31 .. 2026-04-30 and
-- straddle two calendar months, breaking the containment the paragraph above
-- depends on. A one-day period is odd to look at; a period that cannot be
-- filed is a defect.
--
-- So the constraint is relaxed to `>=`. It permits strictly more than before,
-- and reversing it means re-adding the stricter check, which is only possible
-- while no such period exists.

begin;

-- Exclusion constraints below need to combine uuid equality with daterange
-- overlap, which plain gist cannot index.
create extension if not exists btree_gist;

-- ===========================================================================
-- The period scheme belongs to the year
-- ===========================================================================
-- Not to the administration. FR-ONB-007's filing frequency will supply the
-- default, but a business that moves from quarterly to monthly filing does so
-- from a date - and the years already closed keep the periods they were kept
-- in. Storing it on the year is what makes that possible without rewriting
-- history, which is the same argument CMP-014 makes about rates.
alter table fiscal_year
    add column period_scheme text not null default 'monthly'
        check (period_scheme in ('monthly', 'quarterly'));

comment on column fiscal_year.period_scheme is
    'FR-ONB-006. How this year is divided. Per year, not per administration: '
    'a change of filing frequency must not restate closed years.';

-- ---------------------------------------------------------------------------
-- A one-day period (see the header)
-- ---------------------------------------------------------------------------
alter table period drop constraint period_date_order;
alter table period add constraint period_date_order check (end_date >= start_date);

-- ---------------------------------------------------------------------------
-- Years and periods may not overlap
-- ---------------------------------------------------------------------------
-- 0001 has `unique (administration_id, start_date)`, which stops two years
-- starting on the same day and permits 2026-01-01..2026-12-31 to sit on top of
-- 2026-07-01..2027-06-30. A posting dated in the overlap would belong to two
-- fiscal years, and FR-GL-002's "exactly one journal and one period" would be
-- false at the year above it.
--
-- daterange is half-open, so end_date + 1 is what makes a year that ends on
-- the 31st and one that starts on the 1st adjacent rather than overlapping.
alter table fiscal_year
    add constraint fiscal_year_no_overlap
    exclude using gist (
        administration_id with =,
        daterange(start_date, end_date + 1) with &&
    );

alter table period
    add constraint period_no_overlap
    exclude using gist (
        administration_id with =,
        daterange(start_date, end_date + 1) with &&
    );

-- ===========================================================================
-- The derivation, as a function anyone can call
-- ===========================================================================
-- Pure: no tenant, no writes, no reads. That is the point - it is the piece
-- worth testing exhaustively, and tests/ledger/fiscal_cases.py runs the same
-- table against this and against the Python implementation in
-- api/ledger/fiscal.py, so a disagreement between them is a failure rather
-- than a divergence nobody notices.
--
-- Also what an onboarding screen calls to show the periods BEFORE the year is
-- created, which is the difference between choosing a fiscal year and finding
-- out what one did.
create or replace function ledger.derive_fiscal_periods(
    p_start  date,
    p_end    date,
    p_scheme text default 'monthly'
)
returns table (period_number smallint, start_date date, end_date date)
language sql
immutable
as $$
    with recursive block as (
        select 1::smallint as n,
               p_start as s,
               least(
                   case p_scheme
                       when 'quarterly'
                           then (date_trunc('quarter', p_start)
                                 + interval '3 months - 1 day')::date
                       else (date_trunc('month', p_start)
                             + interval '1 month - 1 day')::date
                   end,
                   p_end
               ) as e
         where p_start <= p_end
        union all
        select (b.n + 1)::smallint,
               b.e + 1,
               least(
                   case p_scheme
                       when 'quarterly'
                           then (date_trunc('quarter', b.e + 1)
                                 + interval '3 months - 1 day')::date
                       else (date_trunc('month', b.e + 1)
                             + interval '1 month - 1 day')::date
                   end,
                   p_end
               )
          from block b
         where b.e < p_end
    )
    select b.n, b.s, b.e from block b order by b.n;
$$;

comment on function ledger.derive_fiscal_periods(date, date, text) is
    'FR-ONB-006. The periods a fiscal year is divided into: whole calendar '
    'months or quarters, truncated at the year''s own bounds. Pure - safe to '
    'call to preview a year before creating it.';

-- ===========================================================================
-- Opening a year
-- ===========================================================================
-- The year and its periods in one statement. Separately, an administration
-- could be left with a fiscal year and no periods - which is not a state
-- anything else in this schema knows how to interpret, because every posting
-- names a period (FR-GL-004).
create or replace function ledger.open_fiscal_year(
    p_administration_id uuid,
    p_start_date        date,
    p_end_date          date,
    p_period_scheme     text default 'monthly',
    p_actor_user_id     uuid default null
)
returns fiscal_year
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_admin administration%rowtype;
    v_year  fiscal_year%rowtype;
    v_count integer;
begin
    select * into v_admin from administration where id = p_administration_id;
    if not found then
        raise exception 'administration % does not exist', p_administration_id;
    end if;

    if p_end_date <= p_start_date then
        raise exception
            'a fiscal year ends after it starts: % .. % (FR-ONB-006)',
            p_start_date, p_end_date;
    end if;

    -- A guard rather than a rule of law: the Netherlands permits a first book
    -- year of up to about 24 months, and nothing longer. A 30-year "year" is
    -- a typo, and the cost of catching it here is one comparison.
    if p_end_date > (p_start_date + interval '24 months')::date then
        raise exception
            'a fiscal year of % .. % is longer than 24 months; a long first '
            'book year is permitted, an arbitrary one is not (FR-ONB-006)',
            p_start_date, p_end_date;
    end if;

    insert into fiscal_year (
        organization_id, administration_id, start_date, end_date, period_scheme
    ) values (
        v_admin.organization_id, p_administration_id, p_start_date, p_end_date,
        p_period_scheme
    )
    returning * into v_year;

    insert into period (
        organization_id, administration_id, fiscal_year_id,
        period_number, start_date, end_date
    )
    select v_admin.organization_id, p_administration_id, v_year.id,
           d.period_number, d.start_date, d.end_date
      from ledger.derive_fiscal_periods(p_start_date, p_end_date, p_period_scheme) d;

    get diagnostics v_count = row_count;
    if v_count = 0 then
        raise exception
            'no periods were derived for % .. %, which would leave a year '
            'nothing can be posted into (FR-GL-004)', p_start_date, p_end_date;
    end if;

    return v_year;
end;
$$;

-- ===========================================================================
-- The check on the derivation
-- ===========================================================================
-- Fiscal years whose periods do not tile them exactly: a gap, an overlap, a
-- first period that does not start with the year or a last that does not end
-- with it. Structurally impossible for a year opened through the function
-- above - and `period` has been INSERT-able by ledgr_app since 0001, so years
-- assembled by hand are a state this schema can still reach.
--
-- Built for the reason 0020 gives about its gap report: a check that can only
-- ever be empty is the check on the thing that makes it empty.
create or replace function ledger.fiscal_year_coverage_deviations(
    p_administration_id uuid default null
)
returns table (
    fiscal_year_id    uuid,
    administration_id uuid,
    deviation         text,
    detail            text
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    with bounds as (
        select y.id, y.administration_id, y.start_date, y.end_date,
               count(p.id)          as periods,
               min(p.start_date)    as first_start,
               max(p.end_date)      as last_end,
               -- The days a period covers, against the days the year spans.
               -- Equal only when the periods tile it with no gap and no
               -- overlap, which is the whole property in one comparison.
               coalesce(sum(p.end_date - p.start_date + 1), 0) as covered_days,
               (y.end_date - y.start_date + 1)                 as year_days
          from fiscal_year y
          left join period p on p.fiscal_year_id = y.id
         where p_administration_id is null
            or y.administration_id = p_administration_id
         group by y.id, y.administration_id, y.start_date, y.end_date
    )
    select b.id, b.administration_id,
           case
               when b.periods = 0                    then 'no_periods'
               when b.first_start <> b.start_date    then 'first_period_late'
               when b.last_end <> b.end_date         then 'last_period_short'
               when b.covered_days <> b.year_days    then 'periods_do_not_tile'
           end,
           case
               when b.periods = 0 then
                   'the year has no periods, so nothing can be posted into it'
               when b.first_start <> b.start_date then
                   format('the year starts %s but its first period starts %s',
                          b.start_date, b.first_start)
               when b.last_end <> b.end_date then
                   format('the year ends %s but its last period ends %s',
                          b.end_date, b.last_end)
               else
                   format('the periods cover %s days of a %s day year',
                          b.covered_days, b.year_days)
           end
      from bounds b
     where b.periods = 0
        or b.first_start <> b.start_date
        or b.last_end <> b.end_date
        or b.covered_days <> b.year_days;
$$;

comment on function ledger.fiscal_year_coverage_deviations(uuid) is
    'FR-ONB-006. Fiscal years whose periods do not tile them exactly. Always '
    'empty for a year opened through ledger.open_fiscal_year.';

-- ===========================================================================
-- Ownership and privileges
-- ===========================================================================
alter function ledger.derive_fiscal_periods(date, date, text)   owner to ledgr_ledger;
alter function ledger.open_fiscal_year(uuid, date, date, text, uuid)
    owner to ledgr_ledger;
alter function ledger.fiscal_year_coverage_deviations(uuid)     owner to ledgr_ledger;

-- open_fiscal_year runs as ledgr_ledger and writes both tables. 0001 granted
-- them to ledgr_app, and 0020/0021 gave ledgr_ledger only SELECT on
-- fiscal_year and SELECT/UPDATE on period - so without these the definer
-- function cannot create the year it exists to create.
--
-- No DELETE, matching 0001's stance: a fiscal year is closed, never removed.
grant insert on fiscal_year to ledgr_ledger;
grant insert on period to ledgr_ledger;

-- CMP-014's drift sweep already needs to read periods across tenants (0028);
-- the coverage check needs fiscal_year for the same reason.
grant select on fiscal_year to ledgr_ops;

revoke all on all functions in schema ledger from public;

grant execute on function ledger.derive_fiscal_periods(date, date, text)
    to ledgr_app, ledgr_ops;
grant execute on function ledger.open_fiscal_year(uuid, date, date, text, uuid)
    to ledgr_app;
grant execute on function ledger.fiscal_year_coverage_deviations(uuid)
    to ledgr_app, ledgr_ops;

-- 0020, 0021, 0023, 0024 and 0028's grants are re-issued, following the
-- convention 0021 set: the revoke above strips PUBLIC, and this block keeps
-- the file's grant list the complete picture of who may call what in `ledger`.
grant execute on function ledger.post_entry(
    uuid, uuid, uuid, date, text, text, uuid, text, jsonb, uuid, text, uuid
) to ledgr_app;
grant execute on function ledger.reverse_entry(
    uuid, uuid, date, text, uuid, text, text
) to ledgr_app;
grant execute on function ledger.create_account(
    uuid, text, text, text, text, text, text
) to ledgr_app;
grant execute on function ledger.set_account_status(uuid, text) to ledgr_app;
grant execute on function ledger.set_account_rgs_code(uuid, text) to ledgr_app;
grant execute on function ledger.create_journal(uuid, text, text, text) to ledgr_app;
grant execute on function ledger.set_journal_status(uuid, text) to ledgr_app;
grant execute on function ledger.create_party(uuid, text, text, text) to ledgr_app;
grant execute on function ledger.lock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.unlock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.mark_period_filed(uuid, uuid, text) to ledgr_app;
grant execute on function ledger.open_suppletie(uuid, text, uuid) to ledgr_app;
grant execute on function ledger.close_suppletie(uuid, text, text) to ledgr_app;
grant execute on function ledger.seed_chart_of_accounts(uuid, uuid, uuid, text)
    to ledgr_app;
grant execute on function ledger.apply_rgs_upgrade(uuid, uuid, uuid) to ledgr_app;

grant execute on function ledger.suppletie_corrections(uuid)          to ledgr_app, ledgr_ops;
grant execute on function ledger.numbering_gaps(uuid, uuid)           to ledgr_app, ledgr_ops;
grant execute on function ledger.trial_balance(uuid, uuid)            to ledgr_app, ledgr_ops;
grant execute on function ledger.subledger_balance(uuid, text)        to ledgr_app, ledgr_ops;
grant execute on function ledger.control_account_reconciliation(uuid) to ledgr_app, ledgr_ops;
grant execute on function ledger.money_text(numeric)                  to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_balance(uuid)              to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_control_accounts(uuid)     to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_numbering(uuid)            to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_scope(uuid)                to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_findings(uuid)             to ledgr_app, ledgr_ops;
grant execute on function ledger.plan_rgs_upgrade(uuid, uuid)         to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_readiness(uuid)                  to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_mapping_deviations(uuid)         to ledgr_app, ledgr_ops;
grant execute on function ledger.chart_of_accounts(uuid, boolean)     to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_options(uuid, text)              to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_version_on(date)                 to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_versions_without_effective_from()
    to ledgr_app, ledgr_ops;
grant execute on function ledger.load_rgs_version(jsonb, text, boolean) to ledgr_ops;
grant execute on function ledger.publish_rgs_version(uuid)              to ledgr_ops;

commit;
