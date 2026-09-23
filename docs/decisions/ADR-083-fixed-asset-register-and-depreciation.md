# ADR-083: Fixed-asset register, straight-line depreciation and disposal

- **Status**: Accepted
- **Date**: 2026-09-23

## Context

PRD §13 lists a fixed-asset register among the confirmed stack's scope; Appendix A does not grade
it (no role's capability grid names it). Unlike Journal (ADR-082), nothing here existed yet —
`/assets` was a literal `ComingSoon` stub, with no table, no service, and no permission. A gap
analysis against Exact Online and Yuki named it as one of the four screens with nothing behind
them, and the two competitors both treat an asset register with depreciation as table stakes for
a bookkeeping product.

## Decision

Add migration 0064 (`fixed_asset`, `fixed_asset_depreciation_run`) and a new module,
`api.assets`, structured the way `api.customers` is: `model.py` (value types, validated at
construction), `repository.py` (SQL), `service.py` (business rules), `routes.py` (HTTP). This is
master data pointing AT the ledger, not a second bounded context: depreciation and disposal are
posted by calling `LedgerService.post()` — the same public entry point `api.expenses.posting` and
`api.invoicing.service` already use — never by inventing a second `ledger.*` SQL function or
writing to `journal_entry` directly. `fixed_asset` and `fixed_asset_depreciation_run` are owned by
`ledgr_migrator` with ordinary grants to `ledgr_app`, not by `ledgr_ledger`.

Depreciation is straight-line only (`api.assets.model.DepreciationMethod`, closed like
`JournalType`), one period at a time, triggered explicitly (`POST .../depreciate` names a
`period_id`) — no scheduler, the same posture SI-07's recurring invoices took ("no scheduler: an
explicit call"). The charge is `min(depreciable_amount / useful_life_months, remaining)`, capped
at the remaining depreciable amount rather than indexed by run number, so the asset can never
depreciate below its residual value however many times it is run — see
`api.assets.model.next_depreciation_amount`'s docstring.

Disposal posts a real double-entry write-off (accumulated depreciation cleared, cost removed,
proceeds booked, the gain/loss plugged to a named account) rather than merely flipping a status
flag — `api.assets.service.AssetService.dispose`.

Depreciation and disposal both post into the administration's single active MEMORIAL journal, the
same convention manual journal entries (ADR-082) and bad-debt write-off (ADR-076) already use for
"a posting that is neither a sale, a purchase nor a bank movement."

`api.authz.matrix` gained two EXTENSION_CAPABILITIES — `VIEW_FIXED_ASSET` and
`MANAGE_FIXED_ASSET` — following the "Code purchase invoices"/"View purchase invoices" split
rather than ADR-012's single-capability customer precedent, because a Viewer should be able to
read the register without holding the write. Owner, Accountant and Bookkeeper hold both; Viewer
holds the view only. `scripts/generate_role_catalogue.py` was re-run to keep migration 0010
current for a fresh database; migration 0064 also carries the idempotent `INSERT ... WHERE NOT
EXISTS` backfill migration 0060 pioneered, for a database that already ran an older 0010.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A `ledger.*` SECURITY DEFINER function for depreciation/disposal, the way period locking (0021) works | Period locking changes a column only `ledgr_ledger` may write; depreciation is an ORDINARY posting plus a master-data row update, exactly the shape expense and invoice posting already have a public entry point for. A new SQL function would duplicate `ledger.post_entry` for no reason. |
| Index depreciation charges by run number (1st of 12, 2nd of 12, …) | Ties the charge to a count that must stay in lockstep with `fixed_asset_depreciation_run` rows; capping at the remaining depreciable amount reaches the same total without that bookkeeping, and degrades safely if a period is skipped or the method changes later. |
| A single "Manage fixed assets" capability with no separate view row (ADR-012's customer precedent) | Customers have no PRD-graded read-only role that needs them; Viewer explicitly should see the register (comparable to "View bank transactions" and "View purchase invoices", both R for Viewer in Appendix A) without holding the posting authority. |
| Flip `fixed_asset.status` to `disposed` with no posting | Leaves the asset's cost sitting on the balance sheet forever and books no gain/loss — not a disposal in any accounting sense, and the opposite of CLAUDE.md's "no financial calculation uses floating point... everywhere in the calculation path" spirit applied to correctness generally. |

## Consequences

Makes easier: Bank reconciliation (the next screen) needs the same "post a generic entry into the
single active memorial journal, gated on an open period" shape this module established —
`_single_memorial_journal` and `_open_period_for` in `api.assets.service` are the precedent to
reuse, not necessarily the code (Bank's reconciliation will likely call `LedgerService.post()`
directly from its own service, the same way this module does).

Known gap, left for a follow-up: account-type validation at asset creation (asset/accumulated
accounts must be type `asset`, expense account must be type `expense`) is enforced in
`AssetService.create`, but the `proceeds_account_id`/`gain_loss_account_id` chosen at DISPOSAL
time are not type-checked the same way — a caller could point proceeds at an expense account and
the posting would still balance and post, correctly by double-entry rules but not by the intended
chart-of-accounts discipline. Not blocking (the same trust boundary `EntryInput`/`LineInput`
already extend to every other caller of `LedgerService.post()`), but worth tightening if the
Assets screen sees real use.

**Not yet applied to the local dev database**: migration 0064 could not be applied in this
session — applying a migration as the Postgres superuser is blocked by this environment's
permission classifier (`Modify Shared Resources`), the same restriction prior sessions' memory
already recorded for every migration in the SI-* series. `tests/integration/test_asset_routes.py`
is written and structurally verified (mypy, ruff, and the full non-DB unit suite all pass with the
new module registered), but has not been run against a live Postgres. Apply migration 0064 (and
re-run `scripts/generate_role_catalogue.py`'s output, migration 0010, is NOT re-applied — it was
already current before 0064's idempotent backfill was written) with:

```
psql -h localhost -p 55432 -U postgres -d ledgr -v ON_ERROR_STOP=1 -f apps/api/migrations/0064_fixed_assets.sql
```

then run `TENANT_ISOLATION_TESTS_ENABLED=1 pytest tests/integration/test_asset_routes.py` to
confirm.
