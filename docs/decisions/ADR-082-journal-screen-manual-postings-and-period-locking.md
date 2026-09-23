# ADR-082: Journal screen — manual postings, reversal and period locking

- **Status**: Accepted
- **Date**: 2026-09-23

## Context

FR-GL-001/FR-GL-003/FR-GL-004/FR-GL-007 were fully built at the engine level (`api.ledger.service`,
`api.ledger.periods`) but had no HTTP surface: `/journal` in the web app was a literal `ComingSoon`
stub, and the Grootboek screen's own reversal button was drawn but wired to nothing (its code
comment said so explicitly). A gap analysis against Exact Online and Yuki named this — manual
journal entry, reversing entries, and period locking — as one of the highest-leverage gaps: the
posting engine, the balance/immutability/period-lock invariants, and even the authorization
permissions (`post journal_entry`, `reverse journal_entry`, `lock period`, all already graded in
PRD Appendix A) already existed. Nothing needed to be re-architected; a route needed to be added.

## Decision

Add `api/ledger/routes.py` — the Journal screen's writes — alongside the existing
`api/ledger_reads/routes.py` (the Grootboek screen's reads). It exposes:

- `POST .../journal-entries` — a manual (memorial) posting, built from `EntryInput`/`LineInput`
  exactly as `LedgerService.post()` already required.
- `POST .../journal-entries/{id}/reverse` — FR-GL-003, via the existing `LedgerService.reverse()`.
- `GET .../journals`, `GET .../periods` — pickers the form needs, added as thin new read methods
  on `LedgerService`/`PeriodService` rather than new routes reaching into their repositories.
- `POST .../periods/{id}/lock` and `.../unlock` — FR-GL-007, via `PeriodService`, which already did
  its own fine-grained (period-scoped) authorization check internally.

No migration was needed: every table, trigger, and `ledger.*` SECURITY DEFINER function these
routes call already existed (migrations 0020/0021). This is purely an HTTP surface added on top of
an already-complete, already-tested bounded context.

Both the coarse route-level `require_permission` and `PeriodService`'s own period-scoped check are
declared on the lock/unlock routes — see the module's own docstring for why that is not redundant
(the route-level check satisfies `AuthorizationEnforcementMiddleware`'s "every route declares a
requirement" rule; only `PeriodService`'s internal check can bind an IAM-033 `period_ids`
condition, since the route-level scope API has no way to express "this specific period").

A period-status pre-check was added in the route handlers (rather than relying solely on
`journal_entry_validate()`'s trigger) so a posting into a locked or VAT-filed period returns a
clean, translated 409 instead of a raw database exception reaching the client.

`api.ledger.periods.build_period_service()` was added, mirroring `api.ledger.service.
build_ledger_service()`, so the new routes compose `PeriodService` the same widened-but-narrow way
every other caller outside the bounded context does, rather than importing `SqlPeriodRepository`
directly.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Fold the new writes into `api/ledger_reads/routes.py` | That module is deliberately reads-only (FR-GL-003's reversal is called out in its own docstring as "its own future screen"); mixing writes in would blur a module whose name and docstring both promise otherwise. |
| Let `journal_entry_validate()`'s raw exception surface to the client for a locked period | Produces an unhandled 500 with no translated message — every other refusal in this codebase is a clean `problem()` response; a pre-check against `PeriodService.period()` costs one extra read and avoids that. |
| Skip the route-level `require_permission` on lock/unlock since `PeriodService` already checks | Fails `AuthorizationEnforcementMiddleware` at runtime (500, "declares no authorization requirement") and `tests/test_authz_coverage.py` at build time — every route in this codebase declares its own requirement, with no exemption for a service that checks internally. |

## Consequences

Makes easier: Assets and Bank (the next two ComingSoon screens) both need to post to the ledger the
same way — a fixed-asset depreciation run and a bank-reconciliation "post the difference" action are
both, mechanically, a memorial-journal-shaped posting. They call `LedgerService.post()` directly
(as expense/invoice posting already do) rather than through this HTTP surface, but the period-open
pre-check pattern and the `build_period_service()` composition are now established precedent to
reuse.

Forecloses nothing: `api.ledger.periods.PeriodService.mark_filed`/`open_suppletie` (FR-VAT-005's
suppletie flow) remain deliberately unexposed — they depend on a VAT-return-filing flow that does
not exist yet (FR-VAT-005 is explicitly not built, per `periods.py`'s own module docstring), and
adding a route for them now would let a period be marked `vat_filed` with no return behind the
claim.
