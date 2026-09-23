# ADR-085: Balance sheet and income statement on `ledger.balances_as_of`

- **Status**: Accepted
- **Date**: 2026-09-23

## Context

`/reports` was the fourth `ComingSoon` stub named by the gap analysis against Exact Online and
Yuki. Unlike Journal, Assets and Bank, this one needed neither a migration nor a new permission:
`ledger.balances_as_of` (migration 0062) already returns a cumulative debit-minus-credit balance
per account, as of any date, within a fiscal year, and Appendix A's "View reports" (`view report`)
already gates the trial balance and the journal in `api.ledger_reads.routes`. What was missing was
purely the report shapes and the arithmetic to turn one cumulative-balance read into a balance
sheet and an income statement.

## Decision

Add `api.reports` (`model.py`, `repository.py`, `service.py`, `routes.py`), gated on the existing
`view report` permission, with no migration.

**Balance sheet** = `ledger.balances_as_of(as_of)`, grouped by account type into assets/
liabilities/equity, with revenue and expense balances (also from `balances_as_of`, since the
ledger does not stop counting them at year-end) folded into a `current_year_result` line under
equity — the way any interim balance sheet must: one with no line for the year's result so far
does not balance, because the assets it lists already reflect revenue earned and expense incurred
that no equity account has absorbed yet.

**Income statement** for an arbitrary range = `balances_as_of(period_end)` minus
`balances_as_of(day before period_start)`, per revenue/expense account. `balances_as_of` is
already cumulative from the fiscal year's own start, so the difference of two calls isolates
exactly the requested range without a new SQL shape — the same "reuse the existing read, don't add
a new query" instinct ADR-084 applied to `invoicing.invoice_balances` for bank-match candidates.

The ledger's own debit-minus-credit sign convention (`AccountBalance.balance`) is flipped exactly
once, at the boundary (`api.reports.service`'s `_CREDIT_NORMAL` set), so every figure a reader
sees is positive on its statement's normal side — nothing downstream (the route, the frontend)
needs to know the ledger's internal sign convention.

**Not `api.ledger.chart.ChartOfAccountsService`** for account names/types: that service performs
its OWN internal authorization check against `view chart_of_accounts`, a different permission from
the one this screen is gated on. Reusing it would gate Reports on two permissions by accident
whenever a role's grants on the two rows ever diveraged. `api.reports.repository` instead runs its
own plain `SELECT` on `ledger_account` — not a posting table, so it carries none of
`api.ledger.repository`'s import restriction (CLAUDE.md non-negotiable #1 is about writes to
`journal_entry`/`journal_line`/`journal_sequence`).

## Alternatives considered

| Option | Rejected because |
|---|---|
| A new `ledger.income_statement()` SQL function, mirroring `ledger.trial_balance()` | `balances_as_of` called twice and subtracted in Python already produces the correct figure with no new SQL to write, review, or grant privileges on. |
| Go through `ChartOfAccountsService.chart()` for account metadata | Its internal `view chart_of_accounts` check would gate this screen on a permission other than the one its own route declares (`view report`) - see Decision above. |
| Show the ledger's raw debit-minus-credit balance, unflipped | Would show every liability, equity and revenue account as a negative number to a non-accountant reader - technically correct, practically unreadable, and not what a balance sheet or income statement looks like anywhere else. |
| Omit `current_year_result` from the balance sheet | Produces a balance sheet that does not balance mid-year, which is either confusing or (worse) looks like a real integrity problem the way `ledger.trial_balance`'s own unbalanced-row case is treated as one. |

## Consequences

Makes easier: any future report needing "balance of these accounts as of a date" (a VAT-return
box preview across a period, a budget-vs-actual comparison) has a working example of composing
`balances_as_of` calls rather than writing new ledger-reading SQL.

Verified, unlike Journal/Assets/Bank: this feature needed no migration, so
`tests/integration/test_report_routes.py` (4 tests) was run against the live local Postgres in
this session and passes, including a real balanced-sheet assertion (a 1000.00 capital contribution
posted through `ledger.post_entry` shows as `total_assets == total_equity == "1000.00"`,
`is_balanced: true`) and a real cross-tenant isolation check.

Known gap, left for follow-up: no export (XLSX/CSV/PDF) and no RGS *brugstaat* mapping for either
report — both are read-only JSON today, matching the MVP scope of every other screen this session
built. No comparative period (prior year / prior period side by side), which Exact Online and Yuki
both offer.
