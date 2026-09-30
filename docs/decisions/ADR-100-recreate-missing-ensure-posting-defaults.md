# ADR-100: `app.ensure_posting_defaults` was missing in production; recreated by a new migration

- **Status**: Accepted
- **Date**: 2026-09-30
- **Serves**: FR-EXP-001d (posting), ADR-086 (posting defaults at onboarding), the repair
  attempted in `733939f`/`a0ffa4f` (self-heal a missing posting-account mapping)

## Context

Booking "Mistral AI SAS" in production returned *"This administration has no ledger account
mapped for this posting yet."* A same-session fix made `ExpensePostingService._resolve_accounts`
re-run `app.ensure_posting_defaults` once before refusing (ADR-086's onboarding step, meant to be
idempotent and safe to repeat). Deploying that fix turned the plain refusal into a raw HTTP 500:

```
asyncpg.exceptions.UndefinedFunctionError: function app.ensure_posting_defaults(unknown) does not exist
```

A direct, read-only query against production confirmed it: `pg_proc` has no row named
`ensure_posting_defaults` in schema `app`, at all - not a signature mismatch, not a permissions
issue. The function defined in migration 0067 and redefined in 0070 simply does not exist in this
database.

Railway's `preDeployCommand` (`railway.json`) already runs `scripts/migrate.py` on every deploy,
and migration 0071 (this same session, `bank_transaction_allocation`) applied there without error
- so the tracked runner (ADR-064) is working *now*. The likely history: this database was
bootstrapped, or later baselined into ADR-064's `app.schema_migrations` ledger, at a point that
recorded 0067 as already applied without its body ever actually having run - the `expense_
posting_account` table it also creates already exists and is queried successfully elsewhere, which
is consistent with the table having been created some other way while the function specifically
was not. Re-running 0067 or 0070 is not an option: the runner refuses on a checksum mismatch and
will not re-apply a file it already has recorded, whatever the reason.

## Decision

**Migration 0072 recreates the function**, `create or replace`, with 0070's exact current body -
no logic changes, so it is provably the same behaviour already reviewed and shipped. Whatever left
it missing, the runner **will** apply this new file, because it has never seen this filename
before.

**It also backfills every existing administration**, once, in the same migration: a `do` block
loops over every row in `administration` and calls `app.ensure_posting_defaults` for each. Every
administration onboarded while the function was missing never had it run at all - not only the one
that happened to surface the gap - and the function's own idempotence (fills a gap only, never
changes an existing mapping) makes calling it again for one that is already fine a no-op.

**Verified locally before writing this ADR**: a fresh local Postgres, bootstrapped with
`scripts/bootstrap_production_db.py` running all 72 migration files in order (`0072` included),
applied cleanly, and a direct query afterward confirmed `app.ensure_posting_defaults(uuid)` exists.
This is the same tracked-runner mechanism production uses (ADR-064), exercised end to end rather
than reasoned about.

## Consequences

- Once deployed, `railway up`'s existing `preDeployCommand` applies 0072 automatically - no manual
  `psql` session against production needed for this fix, unlike the diagnostic query that found it.
- The self-heal in `ExpensePostingService._resolve_accounts` (still in place) now has something
  real to call. Its own defensive fallback (`a0ffa4f`: a failed repair never surfaces as a 500)
  stays exactly as it is - a second line of defence, not made redundant by fixing this one gap.
- **Why this happened is still not fully known** - which specific baseline or bootstrap event
  recorded 0067 without running it is not established, only that it must have. If another function
  or object defined in an old migration turns out to be similarly missing, the same pattern
  applies: a new migration recreating it, not an attempt to re-run or edit the old file.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Manually run the function body against production via a one-off `psql` session | Works once, for this one database, and leaves no record in `app.schema_migrations` - the next environment (or a rebuild of this one) hits the identical gap with no trace of how it was fixed last time. A migration is the durable, tracked version of the same fix. |
| Edit `0067_posting_defaults.sql` or `0070_expense_bank_settlement.sql` directly | The runner checksums every already-recorded file and refuses on a mismatch (ADR-064) - editing a file production has already recorded as applied breaks every future deploy against that database, not just this one. |
| Investigate and fix the baselining process itself before patching the symptom | Worth doing eventually, but would leave production broken while that investigation runs. Recreating the function is safe, provably correct (identical body to what already shipped), and unblocks the actual user-facing bug now. |
