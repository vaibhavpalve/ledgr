-- 0062_ledger_balances_as_of.sql
-- FR-UX-005 / MOB-006 (PRD 7.4). See docs/decisions/ADR-080-ledgr-ui-handoff-tokens.md.
--
-- One read-only function on the ledger's public API: the balance of every account
-- with activity in a fiscal year, counting only entries dated on or before a given
-- day. The dashboard uses it for a month-by-month cash position and the change
-- against last month.
--
-- Read-only and additive: no table, no posting, no grant on a posting table. The
-- ledger stays append-only (FR-GL-003, CMP-009) and nothing outside the ledger
-- writes to it (CLAUDE.md, non-negotiable 1).
--
-- Same population as ledger.trial_balance(): entries of that fiscal year, so
-- balances_as_of(admin, year, today) sums to the same figure the trial balance does
-- for the year so far. The dashboard's cash headline and the last point of its
-- history therefore agree by construction.
--
-- SECURITY INVOKER (the default), as 0054's report functions are: the tables' own
-- row-level security applies to the caller, so a tenant sees only its own rows.
-- Unlike trial_balance() it carries no definer privilege, and needs none.
--
-- Reversible: `drop function ledger.balances_as_of(uuid, uuid, date);`

create or replace function ledger.balances_as_of(
    p_administration_id uuid,
    p_fiscal_year_id    uuid,
    p_as_of             date
)
returns table (
    account_id uuid,
    balance    numeric(19, 2)
)
language sql
stable
set search_path = public, app, pg_temp
as $$
    select jl.account_id,
           (coalesce(sum(jl.debit), 0) - coalesce(sum(jl.credit), 0))::numeric(19, 2)
      from journal_line jl
      join journal_entry je on je.id = jl.journal_entry_id
     where je.administration_id = p_administration_id
       and je.fiscal_year_id = p_fiscal_year_id
       and je.entry_date <= p_as_of
     group by jl.account_id;
$$;

comment on function ledger.balances_as_of(uuid, uuid, date) is
    'Balance (debit minus credit) per account for one fiscal year, counting entries dated '
    'on or before p_as_of. Read-only; the same population as ledger.trial_balance().';

alter function ledger.balances_as_of(uuid, uuid, date) owner to ledgr_ledger;

revoke all on function ledger.balances_as_of(uuid, uuid, date) from public;
grant execute on function ledger.balances_as_of(uuid, uuid, date) to ledgr_app, ledgr_ops;
