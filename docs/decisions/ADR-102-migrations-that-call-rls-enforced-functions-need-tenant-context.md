# ADR-102: A migration calling into RLS-enforced ledger functions must set tenant context itself

- **Status**: Accepted
- **Date**: 2026-10-01
- **Serves**: FR-GL (ledger), IAM-001-005 (tenant context), NFR-044 (migrations)
- **Builds on**: [ADR-100](ADR-100-recreate-missing-ensure-posting-defaults.md) (the function this
  fixes), [ADR-064](ADR-064-incremental-migration-runner.md) (the tracked runner this was found
  through)
- **Amends**: nothing architectural - this is a bug in one migration's own backfill, not a change
  to the ledger's API, RLS policies, or the authorization library

## Context

Migration 0072 (ADR-100) recreated `app.ensure_posting_defaults` and backfilled every existing
administration. Applying it against production failed - the whole migration's transaction rolled
back - with `administration <id> does not exist` raised from `ledger.create_journal`, for an
administration the migration's own preceding query had just selected from the `administration`
table in the same transaction.

The row was never missing. `ledger.create_journal` is `security definer`, owned by
`ledgr_migrator`. `administration` carries `force row level security` (0001) specifically because
`ledgr_migrator` is `nosuperuser, nobypassrls` - deliberately not exempt from tenant isolation even
though it owns every tenant table. `administration_select`'s policy is keyed entirely on
`app.current_org_id()`, which reads the session GUC `app.current_org_id` (0001). A real request
sets that GUC per rule #1 before touching any tenant-scoped table; an ad hoc administrative
connection - `scripts/migrate.py` included - never does. Inside `ledger.create_journal`, Postgres
re-evaluates row security as the function's *owner*, not the caller, so this failed identically
whether run by hand or (had the deploy pipeline actually been invoking it - a separate, unrelated
gap) by the tracked runner itself, and would have failed for a literal superuser too, since
`security definer` re-evaluation does not inherit the caller's RLS exemption.

This passed local validation because every locally seeded test administration already had a full
set of journals, so the backfill's `if not exists (...) then perform create_journal(...)` branch
was never actually exercised - the gap only shows up for an administration genuinely missing a
journal, which is exactly the population the backfill exists to reach.

## Decision

**A migration's backfill that walks every tenant and calls into RLS-enforced, tenant-scoped
functions must set that tenant's own context first**, scoped to the current transaction only:

```sql
perform set_config('app.current_org_id', v_admin.organization_id::text, true);
perform app.ensure_posting_defaults(v_admin.id);
```

`true` (transaction-local) matters: it reverts automatically at the migration's own `commit`,
so it cannot leak into whatever runs next in the same connection. This is not a new mechanism -
it is the same GUC the application sets per request - a migration impersonating each tenant in
turn for its own row, one at a time, never another tenant's.

Migration 0075 recreates `app.ensure_posting_defaults` (unchanged - the function itself was never
at fault) and redoes 0072's backfill with this fix. 0072 itself is left as-is, not edited - an
applied migration's checksum is load-bearing (ADR-064); 0075 is a new file, same pattern 0074
already set for superseding a broken predecessor rather than rewriting it.

## Consequences

- Any future migration that backfills through a `security definer`, RLS-enforced function needs
  this same per-tenant `set_config` - this ADR exists so the next person writing one does not
  rediscover the failure mode by breaking production first.
- `app.schema_migrations` now has one row (0072) that does not reflect what the database actually
  contains - its transaction rolled back entirely, backfill included, but recording still happened
  as a separate statement outside that transaction. This is a known, narrow gap in how a *manual*
  catch-up run (not `migrate.py` itself) was applied under time pressure while production was
  broken, not a flaw in the tracked runner's own design - `migrate.py` never produces this
  particular split, since one connection runs both the file and its own bookkeeping insert in
  sequence, and a mid-file crash leaves the *file* un-recorded, not falsely recorded. Harmless
  going forward only because 0075 fully reproduces 0072's intended end state under its own name;
  worth a comment at the row if anyone audits that table later wondering why 0072 "succeeded" with
  no visible effect.
- Confirms the separate, unrelated finding alongside this one: the live Railway service's deploy
  configuration carries no `preDeployCommand` at all, despite `railway.json` specifying one -
  `scripts/migrate.py` has likely never run automatically on any deploy. That is the actual
  deploy-pipeline gap and is tracked and being fixed separately from this migration-authoring bug.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Grant `ledgr_migrator` `BYPASSRLS` | Defeats the entire reason 0001 forced RLS on tenant tables for this role in the first place - every future migration author would lose the safety net that caught this one. |
| Make `ledger.create_journal` tolerant of no tenant context (e.g. fall back to an unscoped lookup) | Would weaken the ledger's own narrow API for every caller, including real requests, to accommodate migrations - the wrong boundary to loosen for a one-off backfill's convenience. |
| Wrap the whole backfill in `set role` to a bypass-capable role instead of setting the GUC | Changes who is running the statements, not what tenant they claim to be acting for - the GUC is the actual mechanism every RLS policy here reads; `set role` would not satisfy it and papers over the real fix. |
