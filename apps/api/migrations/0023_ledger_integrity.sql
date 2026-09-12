-- 0023_ledger_integrity.sql
-- NFR-033 (PRD §12.4). See docs/decisions/ADR-025-ledger-integrity-job.md.
--
--   NFR-033  A nightly integrity job verifies debit/credit balance, sub-ledger
--            to control account agreement, and numbering continuity, alerting
--            on any deviation.
--
-- The three checks correspond to FR-GL-001, FR-GL-006 and FR-GL-013, each of
-- which 0020 already enforces at write time. So every function below should
-- return zero rows, forever, on every deployment.
--
-- ===========================================================================
-- Why build a check for something the schema already prevents
-- ===========================================================================
--
-- 0020's own comment on ledger.numbering_gaps() states the principle and this
-- migration generalises it: "a report that can only ever say 'no gaps' is the
-- check ON the allocator". The write-time guards bind every writer, but they
-- bind them only at write time. What they do not cover:
--
--   * a superuser, who can DISABLE TRIGGER and then write anything. That is
--     the honest limit of every in-database control - 0019 says the same about
--     the audit log - and detection is what remains once prevention is gone.
--   * a restore from a backup taken mid-transaction, a partial replication
--     failover, a data migration that copies entries between databases.
--   * a future migration that drops a trigger, or replaces post_entry with a
--     version missing a check.
--   * silent storage corruption.
--
-- In each of those the ledger is wrong and nothing has raised. NFR-033 exists
-- because "the constraint makes it impossible" is a statement about the code
-- path, and the books are a statement about the rows.
--
-- ===========================================================================
-- SECURITY INVOKER, deliberately, unlike every other function in this schema
-- ===========================================================================
--
-- 0020's functions are SECURITY DEFINER because they WRITE, and the only role
-- holding INSERT on a posting table is ledgr_ledger. These only read, and both
-- roles that call them already hold SELECT. Running them as the invoker is
-- what makes the two callers work correctly:
--
--   ledgr_app   RLS confines the result to the caller's own tenant. An
--               administrator running the check from the application sees
--               deviations in their own books and nobody else's.
--   ledgr_ops   holds BYPASSRLS (0002), so the nightly sweep covers every
--               tenant in one pass with no per-organization loop.
--
-- SECURITY DEFINER would break the second: ledgr_ledger is NOBYPASSRLS and
-- FORCE ROW LEVEL SECURITY applies to it on these tables, so an ops sweep
-- through a definer function would silently report "no deviations" for every
-- tenant - the single worst failure mode available to a check like this one.
--
-- Ownership still moves to ledgr_ledger at the bottom. Ownership is not what
-- runs the query here; it is what stops ledgr_migrator replacing the verifier
-- with one that always reports clean. 0019 owns app.verify_audit_chain() with
-- ledgr_audit for exactly that reason, and
-- tests/integration/test_audit_tamper_evidence.py exercises the attack.
--
-- ===========================================================================
-- This migration is additive (NFR-044)
-- ===========================================================================
--
-- One composite type and seven functions, all new. No table is altered, no
-- existing function is replaced, nothing is dropped. Reversing it is
-- `drop function ...; drop type ledger.integrity_finding;` and the running
-- application is unaffected either way, because nothing in the request path
-- calls any of it.

begin;

-- ===========================================================================
-- The finding shape
-- ===========================================================================
-- A composite type rather than a `returns table (...)` repeated on four
-- functions: the union in ledger.integrity_findings() is then checked by the
-- type system rather than by whoever last edited a column list.
--
-- `detail` is jsonb, and every monetary value inside it is a STRING (see
-- ledger.money_text below). NFR-031 is about the calculation path, and a
-- report is the end of that path: a JSON number here would be parsed as a
-- float by most consumers, and an integrity report that rounds the difference
-- it is reporting is worse than no report.
create type ledger.integrity_finding as (
    -- 'balance' | 'control_account' | 'numbering'. NFR-033's three checks.
    check_name        text,
    -- The requirement the deviation contradicts, for the alert and the
    -- runbook (NFR-045).
    requirement       text,
    -- Which specific deviation within the check. This is the runbook key -
    -- 'entry_unbalanced' and 'orphan_line' are both a balance failure and
    -- have nothing in common as an incident.
    deviation         text,
    organization_id   uuid,
    administration_id uuid,
    subject_type      text,
    subject_id        uuid,
    -- One sentence, readable in an alert without opening the detail.
    summary           text,
    detail            jsonb
);

comment on type ledger.integrity_finding is
    'NFR-033. One deviation found by the ledger integrity job. Every function '
    'returning this should return zero rows on a healthy database.';

-- ---------------------------------------------------------------------------
-- Rendering an amount into the report
-- ---------------------------------------------------------------------------
-- '.' rather than 'D' in the pattern, which matters more than it looks. 'D'
-- is the LOCALE'S decimal separator: under lc_numeric = nl_NL a difference of
-- 0.01 would be reported as "0,01" and any consumer parsing it as a number
-- would read zero, or fail. '.' is the locale-independent decimal point, so
-- this function produces the same string on every server.
--
-- FM strips to_char's leading alignment space; the leading 0 keeps values
-- below 1 readable ("0.01", not ".01"); negatives keep their sign, which they
-- must - the sign of a difference is the direction of the error.
-- STABLE rather than IMMUTABLE: to_char(numeric, text) is itself only stable,
-- because its output can depend on lc_numeric. The pattern above removes that
-- dependence in practice, but labelling a function more permissively than the
-- function it wraps is how a planner ends up constant-folding something it
-- should not have.
create or replace function ledger.money_text(p_amount numeric)
returns text
language sql
stable
as $$
    select to_char(coalesce(p_amount, 0), 'FM9999999999999999990.00');
$$;

-- ===========================================================================
-- Check 1 of 3: debit/credit balance (FR-GL-001)
-- ===========================================================================
-- Three deviations, because "the books balance" fails in three distinguishable
-- ways and each has a different first move for whoever is paged:
--
--   entry_unbalanced           one entry's own debits <> its credits
--   entry_without_lines /
--   entry_with_one_line        a header with nothing, or half of nothing,
--                              posted to it. Sums to zero on both sides, so
--                              the arithmetic check alone waves it through -
--                              the same NULL trap journal_entry_assert_
--                              balanced() guards in 0020.
--   orphan_line                a posting whose entry is gone
--   administration_unbalanced  the aggregate over a whole fiscal year
--
-- The aggregate is not implied by the per-entry checks and is not redundant
-- with them: it is the statement an accountant actually makes about a set of
-- books, and it is what catches rows that no entry accounts for. If it ever
-- fires alone, the per-entry pass missed something and that is itself the
-- finding.
--
-- Every lookup join below is a LEFT JOIN. A finding must never be suppressed
-- because a row it wanted to NAME was missing - dropping the alert for the
-- ledger_journal row is the wrong response to the journal having vanished.
create or replace function ledger.integrity_balance(
    p_administration_id uuid default null
)
returns setof ledger.integrity_finding
language sql
stable
set search_path = public, app, pg_temp
as $$
    with entry_total as (
        select e.id,
               e.organization_id,
               e.administration_id,
               e.fiscal_year_id,
               e.journal_id,
               e.entry_number,
               e.entry_date,
               count(l.id)                as line_count,
               coalesce(sum(l.debit), 0)  as total_debit,
               coalesce(sum(l.credit), 0) as total_credit
          from journal_entry e
          left join journal_line l on l.journal_entry_id = e.id
         where p_administration_id is null
            or e.administration_id = p_administration_id
         group by e.id, e.organization_id, e.administration_id, e.fiscal_year_id,
                  e.journal_id, e.entry_number, e.entry_date
    )
    -- An entry that does not balance on its own.
    select 'balance',
           'FR-GL-001',
           case
               when t.line_count = 0 then 'entry_without_lines'
               when t.line_count = 1 then 'entry_with_one_line'
               else 'entry_unbalanced'
           end,
           t.organization_id,
           t.administration_id,
           'journal_entry',
           t.id,
           case
               when t.line_count = 0 then
                   format('entry %s in journal %s has no posting lines',
                          t.entry_number, coalesce(j.code, t.journal_id::text))
               when t.line_count = 1 then
                   format('entry %s in journal %s has a single line; double-entry '
                          'requires at least two',
                          t.entry_number, coalesce(j.code, t.journal_id::text))
               else
                   format('entry %s in journal %s is unbalanced: debits %s <> credits %s',
                          t.entry_number, coalesce(j.code, t.journal_id::text),
                          ledger.money_text(t.total_debit),
                          ledger.money_text(t.total_credit))
           end,
           jsonb_build_object(
               'journal_id',    t.journal_id,
               'journal_code',  j.code,
               'entry_number',  t.entry_number,
               'entry_date',    t.entry_date,
               'fiscal_year_id', t.fiscal_year_id,
               'line_count',    t.line_count,
               'total_debit',   ledger.money_text(t.total_debit),
               'total_credit',  ledger.money_text(t.total_credit),
               'difference',    ledger.money_text(t.total_debit - t.total_credit)
           )
      from entry_total t
      left join ledger_journal j on j.id = t.journal_id
     where t.line_count < 2
        or t.total_debit <> t.total_credit

    union all

    -- A line whose entry is not there. The FK makes this unreachable through
    -- any statement the database will accept, which is the point of looking:
    -- it is reachable through a restore, a replication gap, or a superuser
    -- who disabled the triggers to remove a header.
    --
    -- Worded as "no visible entry" on purpose. Under RLS as ledgr_app an entry
    -- whose denormalised journal_line.administration_id disagrees with its
    -- parent's presents identically to a missing one, and both are deviations
    -- worth the same page.
    select 'balance',
           'FR-GL-001',
           'orphan_line',
           l.organization_id,
           l.administration_id,
           'journal_line',
           l.id,
           format('posting line %s references journal entry %s, which is not '
                  'visible; the amount is in the books with no entry to explain it',
                  l.id, l.journal_entry_id),
           jsonb_build_object(
               'journal_entry_id', l.journal_entry_id,
               'account_id',       l.account_id,
               'line_number',      l.line_number,
               'debit',            ledger.money_text(l.debit),
               'credit',           ledger.money_text(l.credit)
           )
      from journal_line l
     where not exists (
               select 1 from journal_entry e where e.id = l.journal_entry_id
           )
       and (p_administration_id is null
            or l.administration_id = p_administration_id)

    union all

    -- The aggregate, per administration and fiscal year. FR-GL-001 holding
    -- for every entry implies this holds; the converse is what makes it worth
    -- computing separately.
    select 'balance',
           'FR-GL-001',
           'administration_unbalanced',
           e.organization_id,
           e.administration_id,
           'fiscal_year',
           e.fiscal_year_id,
           format('the books do not balance for fiscal year %s: debits %s <> credits %s '
                  'across %s entries',
                  e.fiscal_year_id,
                  ledger.money_text(coalesce(sum(l.debit), 0)),
                  ledger.money_text(coalesce(sum(l.credit), 0)),
                  count(distinct e.id)),
           jsonb_build_object(
               'fiscal_year_id', e.fiscal_year_id,
               'entries',        count(distinct e.id),
               'total_debit',    ledger.money_text(coalesce(sum(l.debit), 0)),
               'total_credit',   ledger.money_text(coalesce(sum(l.credit), 0)),
               'difference',     ledger.money_text(
                                     coalesce(sum(l.debit), 0)
                                   - coalesce(sum(l.credit), 0))
           )
      from journal_entry e
      left join journal_line l on l.journal_entry_id = e.id
     where p_administration_id is null
        or e.administration_id = p_administration_id
     group by e.organization_id, e.administration_id, e.fiscal_year_id
    having coalesce(sum(l.debit), 0) <> coalesce(sum(l.credit), 0);
$$;

comment on function ledger.integrity_balance(uuid) is
    'NFR-033, FR-GL-001. Returns zero rows when every entry balances, every '
    'entry has lines, no line is orphaned, and each fiscal year nets to zero.';

-- ===========================================================================
-- Check 2 of 3: sub-ledger to control account agreement (FR-GL-006)
-- ===========================================================================
-- FR-GL-006 asks for the sub-ledger to be "reconciled to control accounts
-- continuously". 0020 satisfies that by construction rather than by
-- reconciliation: the sub-ledger IS the control account's own lines grouped by
-- party, so there is no second set of numbers to drift. That construction
-- rests entirely on the biconditional in journal_line_validate() - a line on a
-- control account MUST name a party, and a line anywhere else must NOT.
--
-- So this check is aimed at the construction, not at a reconciliation
-- difference. Four ways it can be false:
--
--   control_line_without_party      a direct posting to a control account, ie.
--                                   an amount owed by nobody
--   party_line_off_control          a receivable recorded where the control
--                                   account cannot see it
--   party_kind_mismatch             a supplier owing money on the AR control
--                                   account, or the reverse
--   party_from_other_administration a line attributing an amount to a party in
--                                   another administration - a tenancy
--                                   deviation that presents as a sub-ledger one
--
-- and one that is a genuine totals comparison:
--
--   control_subledger_difference    the control account's balance against the
--                                   sum of its parties' balances, computed
--                                   through the party join rather than from
--                                   the same rows, so a dangling or foreign
--                                   subledger_party_id makes the two disagree
create or replace function ledger.integrity_control_accounts(
    p_administration_id uuid default null
)
returns setof ledger.integrity_finding
language sql
stable
set search_path = public, app, pg_temp
as $$
    -- FR-GL-006's first half: a control account cannot be posted to directly.
    select 'control_account',
           'FR-GL-006',
           'control_line_without_party',
           l.organization_id,
           l.administration_id,
           'journal_line',
           l.id,
           format('line %s posts %s to the %s control account %s directly, naming no '
                  'sub-ledger party',
                  l.line_number,
                  ledger.money_text(greatest(l.debit, l.credit)),
                  a.control_kind, a.code),
           jsonb_build_object(
               'journal_entry_id', l.journal_entry_id,
               'account_id',       a.id,
               'account_code',     a.code,
               'control_kind',     a.control_kind,
               'debit',            ledger.money_text(l.debit),
               'credit',           ledger.money_text(l.credit)
           )
      from journal_line l
      join ledger_account a on a.id = l.account_id
     where a.control_kind is not null
       and l.subledger_party_id is null
       and (p_administration_id is null
            or l.administration_id = p_administration_id)

    union all

    -- The converse half, which matters as much: an amount attributed to a
    -- party but posted somewhere the control account does not aggregate.
    select 'control_account',
           'FR-GL-006',
           'party_line_off_control',
           l.organization_id,
           l.administration_id,
           'journal_line',
           l.id,
           format('line %s names sub-ledger party %s on account %s, which is not a '
                  'control account; the amount is attributed to a party the control '
                  'account cannot see',
                  l.line_number, coalesce(p.name, l.subledger_party_id::text), a.code),
           jsonb_build_object(
               'journal_entry_id',   l.journal_entry_id,
               'account_id',         a.id,
               'account_code',       a.code,
               'subledger_party_id', l.subledger_party_id,
               'party_name',         p.name,
               'debit',              ledger.money_text(l.debit),
               'credit',             ledger.money_text(l.credit)
           )
      from journal_line l
      join ledger_account a on a.id = l.account_id
      left join subledger_party p on p.id = l.subledger_party_id
     where a.control_kind is null
       and l.subledger_party_id is not null
       and (p_administration_id is null
            or l.administration_id = p_administration_id)

    union all

    -- A customer on the payables control account, or a supplier on
    -- receivables. The totals still balance; the sub-ledger is nonsense.
    select 'control_account',
           'FR-GL-006',
           'party_kind_mismatch',
           l.organization_id,
           l.administration_id,
           'journal_line',
           l.id,
           format('line %s posts %s party %s to the %s control account %s',
                  l.line_number, p.party_kind, p.name, a.control_kind, a.code),
           jsonb_build_object(
               'journal_entry_id',   l.journal_entry_id,
               'account_code',       a.code,
               'control_kind',       a.control_kind,
               'subledger_party_id', p.id,
               'party_kind',         p.party_kind,
               'party_name',         p.name
           )
      from journal_line l
      join ledger_account a on a.id = l.account_id
      join subledger_party p on p.id = l.subledger_party_id
     where a.control_kind is not null
       and ((a.control_kind = 'accounts_receivable' and p.party_kind <> 'customer')
         or (a.control_kind = 'accounts_payable'    and p.party_kind <> 'supplier'))
       and (p_administration_id is null
            or l.administration_id = p_administration_id)

    union all

    -- CLAUDE.md rule 1 as it surfaces inside FR-GL-006: an amount owed by a
    -- party belonging to a different administration.
    --
    -- The join to subledger_party is an inner one, so a party in another
    -- ORGANIZATION - invisible to this caller under RLS - drops out of this
    -- branch entirely rather than being named. That case is not lost: the
    -- totals branch below counts only lines reaching a party in this
    -- administration, so an unreachable party leaves the control account and
    -- its sub-ledger disagreeing. Two branches, and the one that cannot be
    -- silenced by visibility is the one that always fires.
    select 'control_account',
           'FR-GL-006',
           'party_from_other_administration',
           l.organization_id,
           l.administration_id,
           'journal_line',
           l.id,
           format('line %s attributes %s to party %s, which belongs to administration '
                  '%s rather than %s',
                  l.line_number,
                  ledger.money_text(greatest(l.debit, l.credit)),
                  p.name, p.administration_id, l.administration_id),
           jsonb_build_object(
               'journal_entry_id',        l.journal_entry_id,
               'account_code',            a.code,
               'subledger_party_id',      p.id,
               'party_administration_id', p.administration_id,
               'line_administration_id',  l.administration_id
           )
      from journal_line l
      join ledger_account a on a.id = l.account_id
      join subledger_party p on p.id = l.subledger_party_id
     where p.administration_id <> l.administration_id
       and (p_administration_id is null
            or l.administration_id = p_administration_id)

    union all

    -- The totals comparison FR-GL-006 names. The two sides are deliberately
    -- computed by different routes: the control side from the account's own
    -- lines, the sub-ledger side only from lines that reach a real party in
    -- this administration. Identical by construction, and therefore a
    -- difference means the construction is broken - which is exactly what
    -- this job is for.
    select 'control_account',
           'FR-GL-006',
           'control_subledger_difference',
           a.organization_id,
           a.administration_id,
           'ledger_account',
           a.id,
           format('control account %s carries %s but its sub-ledger accounts for %s; '
                  '%s is unattributed',
                  a.code,
                  ledger.money_text(coalesce(sum(l.debit) - sum(l.credit), 0)),
                  ledger.money_text(coalesce(
                      sum(l.debit)  filter (where p.id is not null)
                    - sum(l.credit) filter (where p.id is not null), 0)),
                  ledger.money_text(
                      coalesce(sum(l.debit) - sum(l.credit), 0)
                    - coalesce(sum(l.debit)  filter (where p.id is not null)
                             - sum(l.credit) filter (where p.id is not null), 0))),
           jsonb_build_object(
               'account_code',      a.code,
               'control_kind',      a.control_kind,
               'control_balance',   ledger.money_text(
                                        coalesce(sum(l.debit) - sum(l.credit), 0)),
               'subledger_balance', ledger.money_text(coalesce(
                                        sum(l.debit)  filter (where p.id is not null)
                                      - sum(l.credit) filter (where p.id is not null), 0)),
               'difference',        ledger.money_text(
                                        coalesce(sum(l.debit) - sum(l.credit), 0)
                                      - coalesce(sum(l.debit)  filter (where p.id is not null)
                                               - sum(l.credit) filter (where p.id is not null), 0))
           )
      from ledger_account a
      left join journal_line l on l.account_id = a.id
      left join subledger_party p
             on p.id = l.subledger_party_id
            and p.administration_id = a.administration_id
     where a.control_kind is not null
       and (p_administration_id is null
            or a.administration_id = p_administration_id)
     group by a.id, a.organization_id, a.administration_id, a.code, a.control_kind
    having coalesce(sum(l.debit) - sum(l.credit), 0)
        <> coalesce(sum(l.debit)  filter (where p.id is not null)
                  - sum(l.credit) filter (where p.id is not null), 0);
$$;

comment on function ledger.integrity_control_accounts(uuid) is
    'NFR-033, FR-GL-006. Returns zero rows when every control account line '
    'names a party of the right kind in the right administration, no other '
    'line names a party, and each control balance equals its sub-ledger.';

-- ===========================================================================
-- Check 3 of 3: numbering continuity (FR-GL-013, CMP-009)
-- ===========================================================================
-- ledger.numbering_gaps() already reports holes for ONE (journal, year).
-- NFR-033 needs the sweep across every series, and it needs three things that
-- report cannot see:
--
--   numbering_gap     a hole inside a series. One finding per SERIES, not per
--                     missing number: a series with a thousand holes is one
--                     incident, and a thousand pages is an alert nobody reads
--                     (NFR-045 - alerts that cannot be acted on are deleted).
--
--   sequence_drift    journal_sequence.next_number disagrees with max+1. This
--                     is the check that catches a deleted TAIL, which the gap
--                     report structurally cannot: delete entries 4 and 5 and
--                     the remaining series 1..3 is perfectly gapless. The
--                     allocator row is to a numbering series what an external
--                     anchor (IAM-092) is to the audit chain - a record of how
--                     far the series got, kept where the deletion did not
--                     reach.
--
--   sequence_missing  entries exist for a series with no allocator row at all.
--
--   series_missing    the mirror: the allocator says numbers were issued and
--                     not one entry remains. A whole journal-year deleted.
--
-- The gap enumeration is guarded by `entries <> highest`, which is not an
-- optimisation but the invariant itself: entry_number is UNIQUE per
-- (journal, fiscal_year) and CHECKed >= 1, so count = max holds if and only if
-- the series is exactly 1..max. Healthy series - all of them, always - are
-- therefore rejected by a comparison of two aggregates, and generate_series is
-- only ever expanded over a series already known to be broken.
create or replace function ledger.integrity_numbering(
    p_administration_id uuid default null
)
returns setof ledger.integrity_finding
language sql
stable
set search_path = public, app, pg_temp
as $$
    with series as (
        select e.organization_id,
               e.administration_id,
               e.journal_id,
               e.fiscal_year_id,
               count(*)              as entries,
               min(e.entry_number)   as lowest,
               max(e.entry_number)   as highest
          from journal_entry e
         where p_administration_id is null
            or e.administration_id = p_administration_id
         group by e.organization_id, e.administration_id, e.journal_id,
                  e.fiscal_year_id
    )
    select 'numbering',
           'FR-GL-013',
           'numbering_gap',
           s.organization_id,
           s.administration_id,
           'ledger_journal',
           s.journal_id,
           format('journal %s, fiscal year %s: %s entries numbered 1..%s, so %s '
                  'number(s) are missing from the series',
                  coalesce(j.code, s.journal_id::text), s.fiscal_year_id,
                  s.entries, s.highest, s.highest - s.entries),
           jsonb_build_object(
               'journal_code',   j.code,
               'fiscal_year_id', s.fiscal_year_id,
               'entries',        s.entries,
               'lowest_number',  s.lowest,
               'highest_number', s.highest,
               'missing_count',  s.highest - s.entries,
               -- Capped. The count above is the whole truth; this is the
               -- sample whoever is paged starts from.
               'missing_sample', (
                    select jsonb_agg(m.n order by m.n)
                      from (
                            select g.n
                              from generate_series(1, s.highest) as g(n)
                             where not exists (
                                       select 1
                                         from journal_entry x
                                        where x.journal_id = s.journal_id
                                          and x.fiscal_year_id = s.fiscal_year_id
                                          and x.entry_number = g.n
                                   )
                             order by g.n
                             limit 50
                           ) m
               )
           )
      from series s
      left join ledger_journal j on j.id = s.journal_id
     where s.entries <> s.highest

    union all

    -- The allocator against the series it allocated for.
    select 'numbering',
           'FR-GL-013',
           case when q.journal_id is null then 'sequence_missing'
                else 'sequence_drift' end,
           s.organization_id,
           s.administration_id,
           'ledger_journal',
           s.journal_id,
           case
               when q.journal_id is null then
                   format('journal %s, fiscal year %s: %s entries numbered up to %s, '
                          'but the series has no allocator row - the next posting '
                          'would restart at 1',
                          coalesce(j.code, s.journal_id::text), s.fiscal_year_id,
                          s.entries, s.highest)
               when q.next_number > s.highest + 1 then
                   format('journal %s, fiscal year %s: the allocator has issued up to '
                          '%s but the highest entry is %s - %s number(s) were issued '
                          'to entries that are no longer there',
                          coalesce(j.code, s.journal_id::text), s.fiscal_year_id,
                          q.next_number - 1, s.highest,
                          q.next_number - 1 - s.highest)
               else
                   format('journal %s, fiscal year %s: the allocator will issue %s but '
                          'the highest entry is already %s - the next posting would '
                          'collide with an existing number',
                          coalesce(j.code, s.journal_id::text), s.fiscal_year_id,
                          q.next_number, s.highest)
           end,
           jsonb_build_object(
               'journal_code',   j.code,
               'fiscal_year_id', s.fiscal_year_id,
               'entries',        s.entries,
               'highest_number', s.highest,
               'next_number',    q.next_number,
               'expected_next',  s.highest + 1
           )
      from series s
      left join ledger_journal j on j.id = s.journal_id
      left join journal_sequence q
             on q.journal_id = s.journal_id
            and q.fiscal_year_id = s.fiscal_year_id
     where q.journal_id is null
        or q.next_number <> s.highest + 1

    union all

    -- An allocator that issued numbers to a series with nothing left in it.
    -- next_number = 1 is an untouched allocator and not a deviation; anything
    -- above it means entries existed and do not now.
    select 'numbering',
           'FR-GL-013',
           'series_missing',
           q.organization_id,
           q.administration_id,
           'ledger_journal',
           q.journal_id,
           format('journal %s, fiscal year %s: the allocator has issued %s number(s) '
                  'but the series holds no entries at all',
                  coalesce(j.code, q.journal_id::text), q.fiscal_year_id,
                  q.next_number - 1),
           jsonb_build_object(
               'journal_code',   j.code,
               'fiscal_year_id', q.fiscal_year_id,
               'next_number',    q.next_number,
               'issued',         q.next_number - 1,
               'entries',        0
           )
      from journal_sequence q
      left join ledger_journal j on j.id = q.journal_id
     where q.next_number > 1
       and not exists (
               select 1
                 from journal_entry e
                where e.journal_id = q.journal_id
                  and e.fiscal_year_id = q.fiscal_year_id
           )
       and (p_administration_id is null
            or q.administration_id = p_administration_id);
$$;

comment on function ledger.integrity_numbering(uuid) is
    'NFR-033, FR-GL-013, CMP-009. Returns zero rows when every (journal, '
    'fiscal year) series is exactly 1..n with an allocator standing at n+1.';

-- ===========================================================================
-- What the sweep examined
-- ===========================================================================
-- The counts are not decoration and not telemetry. Every function above
-- reports a deviation by RETURNING A ROW, so "no rows" means both "clean" and
-- "looked at nothing" - and the two are indistinguishable to a caller that
-- only counts findings. A job that reports success because RLS hid every row
-- from it is the failure mode this whole migration exists to avoid, so the
-- scope travels with the report and the caller can refuse to call an empty
-- sweep a pass. scripts/verify_ledger_integrity.py exits 3 on it.
create or replace function ledger.integrity_scope(
    p_administration_id uuid default null
)
returns table (
    administrations bigint,
    journals        bigint,
    accounts        bigint,
    entries         bigint,
    lines           bigint
)
language sql
stable
set search_path = public, app, pg_temp
as $$
    select
        (select count(*) from administration a
          where p_administration_id is null or a.id = p_administration_id),
        (select count(*) from ledger_journal j
          where p_administration_id is null
             or j.administration_id = p_administration_id),
        (select count(*) from ledger_account c
          where p_administration_id is null
             or c.administration_id = p_administration_id),
        (select count(*) from journal_entry e
          where p_administration_id is null
             or e.administration_id = p_administration_id),
        (select count(*) from journal_line l
          where p_administration_id is null
             or l.administration_id = p_administration_id);
$$;

comment on function ledger.integrity_scope(uuid) is
    'NFR-033. What the caller could see. administrations = 0 means the sweep '
    'examined nothing, which is not the same as finding nothing.';

-- ===========================================================================
-- The job, as one call
-- ===========================================================================
create or replace function ledger.integrity_findings(
    p_administration_id uuid default null
)
returns setof ledger.integrity_finding
language sql
stable
set search_path = public, app, pg_temp
as $$
    select * from ledger.integrity_balance(p_administration_id)
    union all
    select * from ledger.integrity_control_accounts(p_administration_id)
    union all
    select * from ledger.integrity_numbering(p_administration_id)
    -- By position: 1 = check_name, 5 = administration_id, 3 = deviation. A
    -- stable order means two runs over unchanged books produce byte-identical
    -- reports, which is what lets a diff of two runs be meaningful.
    order by 1, 5, 3, 7;
$$;

comment on function ledger.integrity_findings(uuid) is
    'NFR-033. The nightly integrity job: balance (FR-GL-001), sub-ledger to '
    'control account agreement (FR-GL-006) and numbering continuity '
    '(FR-GL-013). Zero rows is the only healthy result. Pair it with '
    'ledger.integrity_scope() - no rows from an empty scope is not a pass.';

-- ===========================================================================
-- Ownership and privileges
-- ===========================================================================
-- Owned by ledgr_ledger so that ledgr_migrator - which owns most of this
-- schema and can CREATE OR REPLACE anything it owns - cannot replace a check
-- with one that returns nothing. That is the "replace the verifier so
-- tampering looks clean" attack enumerated in
-- tests/integration/test_audit_tamper_evidence.py, and the answer here is the
-- answer 0019 gave there: an owner that is neither the migration role nor the
-- application role.
--
-- Ownership does NOT change how these run - they are SECURITY INVOKER, see the
-- header - which is precisely the combination wanted: the caller's visibility,
-- and nobody's ability to quietly neuter the check.
alter type     ledger.integrity_finding                    owner to ledgr_ledger;
alter function ledger.money_text(numeric)                  owner to ledgr_ledger;
alter function ledger.integrity_balance(uuid)              owner to ledgr_ledger;
alter function ledger.integrity_control_accounts(uuid)     owner to ledgr_ledger;
alter function ledger.integrity_numbering(uuid)            owner to ledgr_ledger;
alter function ledger.integrity_scope(uuid)                owner to ledgr_ledger;
alter function ledger.integrity_findings(uuid)             owner to ledgr_ledger;

-- Postgres grants EXECUTE on a new function to PUBLIC. 0020 revoked that for
-- the functions that existed then; these are new, so the default is back and
-- has to be revoked again before the audience is named.
revoke all on all functions in schema ledger from public;

-- Both callers, and only these two.
--
--   ledgr_app  an administrator running the check over their own books. RLS
--              scopes the answer to their tenant, which is what CLAUDE.md
--              rule 1 requires of every query path including this one.
--   ledgr_ops  the nightly sweep. BYPASSRLS covers every tenant in one pass.
--              Read-only, like every other privilege this role holds on the
--              ledger (CMP-009: support tooling reads and never writes).
--
-- Each function is granted individually rather than schema-wide: the write
-- functions live in this schema too, and `grant execute on all functions in
-- schema ledger` would hand ledgr_ops post_entry.
grant execute on function ledger.money_text(numeric)              to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_balance(uuid)          to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_control_accounts(uuid) to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_numbering(uuid)        to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_scope(uuid)            to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_findings(uuid)         to ledgr_app, ledgr_ops;

-- 0020's and 0021's grants are re-issued, following the convention 0021 set:
-- the revoke above strips PUBLIC, and an explicit grant to ledgr_app made in
-- an earlier migration survives it, but listing them keeps this file's grant
-- block the complete picture of who may call what in the `ledger` schema
-- rather than a delta a reader has to compose with two other files.
--
-- The absences are the point of reading it: ledgr_ops appears on every
-- reporting function and on none of the write ones (CMP-009), and no role
-- other than these two appears at all.
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
grant execute on function ledger.create_journal(uuid, text, text, text) to ledgr_app;
grant execute on function ledger.set_journal_status(uuid, text) to ledgr_app;
grant execute on function ledger.create_party(uuid, text, text, text) to ledgr_app;
grant execute on function ledger.lock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.unlock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.mark_period_filed(uuid, uuid, text) to ledgr_app;
grant execute on function ledger.open_suppletie(uuid, text, uuid) to ledgr_app;
grant execute on function ledger.close_suppletie(uuid, text, text) to ledgr_app;

grant execute on function ledger.suppletie_corrections(uuid)             to ledgr_app, ledgr_ops;
grant execute on function ledger.numbering_gaps(uuid, uuid)              to ledgr_app, ledgr_ops;
grant execute on function ledger.trial_balance(uuid, uuid)               to ledgr_app, ledgr_ops;
grant execute on function ledger.subledger_balance(uuid, text)           to ledgr_app, ledgr_ops;
grant execute on function ledger.control_account_reconciliation(uuid)    to ledgr_app, ledgr_ops;

commit;
