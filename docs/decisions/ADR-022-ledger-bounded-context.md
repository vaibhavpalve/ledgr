# ADR-022: The ledger as a bounded context with a narrow API

- **Status**: Accepted
- **Date**: 2026-09-01
- **Implements**: FR-GL-001 – FR-GL-007, FR-GL-013 (PRD §6.2), CMP-009, NFR-031, NFR-032, NFR-042
- **Supersedes nothing.** Extends the ownership pattern from [ADR-020](ADR-020-audit-log.md).

## Context

> **CLAUDE.md, architectural non-negotiable #1.** The ledger is a separate bounded context with a
> narrow API. Nothing writes to posting tables except the ledger service.

That sentence is usually implemented as a module boundary: a service class, a code review rule,
maybe an import lint. All three are advisory. A developer who needs a query the service does not
expose reaches for the repository, and the context stops being one — not through a decision, but
through convenience.

So the question this ADR answers is not "where does the ledger live" but **"what stops the second
writer"** — and the answer has to survive a developer who has not read this file.

## Decision

### The bounded context is a privilege boundary, not a package boundary

`ledgr_app` — the role every request runs as — holds **SELECT and nothing else** on
`journal_entry`, `journal_line`, `journal_sequence`, `ledger_account`, `ledger_journal` and
`subledger_party`. No INSERT, no UPDATE, no DELETE, no TRUNCATE.

The only writers are the `SECURITY DEFINER` functions in the `ledger` schema, which run as
`ledgr_ledger` — the tables' owner, NOLOGIN, granted to nobody. Their signatures **are** the narrow
API, and the surface a reviewer has to audit for "can this write a bad posting" is exactly their
bodies:

```
ledger.post_entry      ledger.create_account       ledger.numbering_gaps
ledger.reverse_entry   ledger.set_account_status   ledger.trial_balance
                       ledger.create_journal       ledger.subledger_balance
                       ledger.set_journal_status   ledger.control_account_reconciliation
                       ledger.create_party
```

Application code that ignores every convention and writes its own INSERT gets `permission denied
for table journal_entry`. That is a stronger statement than any lint, and it is the reason the
Python-level checks in `tests/ledger/test_bounded_context.py` are described there as ergonomics
rather than as the guarantee.

**Tenant isolation survives the elevation** because RLS predicates read `app.current_org_id()`,
which is *session* state rather than *role* state. A definer function called by tenant A sees
tenant A's rows and no others. `FORCE ROW LEVEL SECURITY` is what makes that bind `ledgr_ledger`
too, despite it owning the tables — without it Postgres exempts owners and every one of these
functions becomes a cross-tenant read path.

**The chart of accounts is inside the boundary**, which is not obvious, because it is not a posting
table. It is there because FR-GL-006 makes it load-bearing: code that could INSERT into
`ledger_account` directly could create an ordinary account, post to it freely, and only then mark
it as the receivables control account. Every posting would have been legal when it was made and the
administration would end up with a control account full of direct postings.

### Why each invariant holds structurally

| Invariant | Mechanism | Why not in application code |
|---|---|---|
| Debits = credits (FR-GL-001) | Deferred constraint trigger at COMMIT | Lines arrive one INSERT at a time; an immediate check rejects the first line of every entry |
| ≥ 2 lines, non-empty | Same trigger | `SUM()` over no rows is NULL, `NULL <> 0` is not TRUE — an empty entry passes a naive balance check |
| One-sided lines | `CHECK ((debit = 0) <> (credit = 0))` | Cheapest possible place; makes the invalid state unrepresentable |
| Immutability (FR-GL-003) | No grants + RAISE triggers + NOLOGIN owner | The threat model includes someone holding a database connection |
| Sealed at commit | `created_xid = pg_current_xact_id()` on line insert | Withheld UPDATE/DELETE stops *changing* an entry, not *appending* to one |
| Reversal mirrors | Deferred constraint trigger | A "reversal" that quietly differs is the manipulation the requirement exists to prevent |
| Reverse-once | Partial unique index | Two concurrent reversals both pass an application check |
| Control accounts (FR-GL-006) | BEFORE INSERT trigger, biconditional | Needs a cross-table lookup, so not expressible as a CHECK |
| Gapless numbers (FR-GL-013) | Allocator row + UNIQUE index | A Postgres `SEQUENCE` is deliberately non-transactional |
| Period open (FR-GL-007) | BEFORE INSERT trigger | — |
| Decimal only (NFR-031) | `numeric(19,2)`; jsonb amounts must be strings | A float has lost precision before any check can see it |

### The three that deserve their own paragraph

**The empty-entry trap.** `SUM()` over zero rows returns `NULL`, and `NULL <> 0` evaluates to
`NULL`, which is not `TRUE`. A balance trigger written the obvious way lets a header with no
postings commit. The line count in `journal_entry_assert_balanced()` is not defensive decoration —
it is the check, and the same trap is guarded separately in the in-memory fake and in
`EntryInput.assert_balanced`.

**Sealing at commit.** Immutability of existing rows says nothing about *appending* a line to
yesterday's entry, which unbalances it exactly as effectively as editing one — and the deferred
balance trigger has already fired and will not fire again. Storing `pg_current_xact_id()` on the
header and comparing it on every line insert means lines can only be added by the transaction that
created the header. An entry is sealed the instant it commits. This is the piece a
privileges-and-triggers review misses, because nothing about it looks like mutation.

**The sub-ledger is derived, not stored.** FR-GL-006 asks for AR/AP sub-ledgers "reconciled to
control accounts continuously". The obvious reading is a reconciliation job. Instead, a line on a
control account is *required* to name a party and a line anywhere else is *forbidden* to — so the
sub-ledger is the control account's own lines grouped by party. There is no second set of numbers,
nothing to drift, and no job that can fall behind.
`ledger.control_account_reconciliation()` reports a difference that is structurally zero; a non-zero
row means the trigger has been removed, which is the only thing left for it to detect.

### Numbering: the cost is real and is the point

`journal_sequence` is a table with an `UPDATE … RETURNING`, not a `SEQUENCE`. Postgres sequences
keep their consumed value across a rollback by design — correct for surrogate keys, disqualifying
for a statutory series where CMP-009 requires gaplessness "sufficient to satisfy inspection
standards".

**The consequence: concurrent postings to the same journal in the same year serialise on one row.**
That is the price of gaplessness. It is scoped as narrowly as the requirement permits — per
(journal, fiscal year), not per administration — and it is stated here rather than discovered under
load.

The idempotency check (NFR-032) runs *before* the allocator, so a retried request does not consume
a number. Running it after would punch holes in the very series FR-GL-013 requires to be gapless.

### Decimal, and where the tripwire goes

`numeric(19,2)`. Postgres stores a jsonb number as `numeric`, so the database is not where
precision is lost — the loss happens on the way in, when `json.dumps()` writes a Python float.
So `ledger.post_entry` **requires amounts to be JSON strings and rejects JSON numbers**, and
`api.ledger.model.amount_to_string` rejects `float` rather than coercing it. A float that reaches
either boundary has already lost the precision; `4335.09 + 2964.61` is `7299.700000000001`, small
enough to survive review and large enough to make a trial balance fail to net to zero.

Amounts with more than two decimals are **rejected, not rounded**. A posted amount is already
settled, and which way to round is a decision for the calculation that produced it — VAT has its
own rule — not for the boundary that stores it.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A service class plus an import lint, tables writable by `ledgr_app` | Advisory. The first developer who needs an unexposed query writes their own INSERT, and nothing fails. |
| A `SEQUENCE` for entry numbers | Non-transactional by design; a rolled-back post leaves a permanent hole in a series that must be gapless (CMP-009). |
| A stored `subledger_balance` reconciled by a job | Creates a second set of numbers that can drift, and a job that can fall behind. Deriving it makes FR-GL-006 true by construction. |
| A `reversed_by_entry_id` back-pointer on the original | Requires an UPDATE to a committed row — exactly what FR-GL-003 forbids. "Has this been reversed" is an index lookup on `reverses_entry_id`. |
| Letting the caller supply the reversal's lines | `journal_entry_reverses_trg` would still reject a wrong mirror, but a caller that cannot supply them cannot get them wrong — and the caller is sometimes a support tool under time pressure. |
| A signed `amount` column instead of debit/credit | Loses the distinction a reader checking a journal against a bank statement actually uses, and the one-sided CHECK stops meaning anything. |
| Checking the balance in `post_entry` rather than in a deferred trigger | Would bind only callers of that function. The trigger binds any writer, including the function itself. |
| `numeric(19,4)` now, to accommodate FR-GL-010 | Lets a 4dp amount be posted that does not represent real money. Multi-currency adds transaction-currency columns *beside* these instead. |
| Leaving the tables owned by `ledgr_migrator` | An owner may `DROP TRIGGER`, which makes the immutability layer decorative against a migration — the case CMP-009 names. |
| Omitting `revoke all on all functions in schema ledger from public` | Postgres grants EXECUTE to PUBLIC by default, which would hand `ledgr_ops` the write functions and make CMP-009's "including support tooling" false for insertion. |

## Consequences

- **91 DB-gated integration tests** across three files: `test_ledger_bounded_context.py` (the
  withheld grants, enumerated from the catalogue so a role added later is covered the day it
  appears), `test_ledger_invariants.py` (the shared case table), `test_ledger_immutability.py`
  (14 statement forms × `ledgr_app` and `ledgr_ops`).
- **One case table, executed twice.** `tests/ledger/cases.py` runs against the in-memory fake and
  against Postgres, matching one substring against both error messages. That is what keeps the fake
  honest — the drift risk ADR-020 handled by comparing the fake's hash to Postgres's.
- **20 guards mutation-tested, all caught** by `tests/ledger`. Including the two that are about
  *ordering* rather than presence: an allocator that skips a number, and an idempotency check moved
  after allocation. Run against `test_properties.py` alone, 19 of the 20 are caught; the survivor is
  "a posting writes no audit entry", which is not one of NFR-042's invariants and is caught by
  `tests/audit/test_wiring.py`.
- **Two mutations initially passed for the wrong reason, and both were the tests' fault.** The audit
  mutation left an `await None`, so it failed on `TypeError` rather than on a missing audit entry.
  And the period-integrity property asserted only that *a* `LedgerError` was raised — with the
  period-status guard removed, the entry-date check refused the same entry and the test could not
  tell the difference. Both now assert the *reason*, the same discipline `tests/ledger/cases.py`
  applies with its `expect` substrings.
- **NFR-042's property tests use Hypothesis** (`tests/ledger/test_properties.py`). Balance,
  sub-ledger equality and reversal symmetry are `@given` properties over generated entries;
  immutability, gaplessness and period integrity are `@invariant`s on a `RuleBasedStateMachine`,
  because none of those three is a property of a single input — an entry is immutable *across
  everything that happens afterwards*, and a numbering series is only ever wrong on the failure
  path. `derandomize=True` so a CI failure reproduces locally.
- **Adding Hypothesis surfaced that CI had never installed anything.** `uv sync --frozen` runs in
  four jobs and there was no `uv.lock` in the repository at all, so every API job failed at the
  install step. `uv.lock` is now generated (85 packages) and `UV_VERSION` moved from `0.5.4` to
  `0.12.8`, which is what can read a revision-3 lockfile. This predates the ledger and is unrelated
  to it; it was found by trying to add a dependency.
- **`AuditCategory.POSTING` finally has a producer.** It has been declared with no caller since
  0019. The audit entry is written in the same transaction as the posting, so a posting without one
  is not a reachable state — and a posting deleted behind the ledger's back leaves an audit entry
  pointing at nothing, which is the ledger's only detection story against a superuser.
- **Two reporting bugs were found by writing the tests, not by reading the code.**
  `ledger.trial_balance` filtered the fiscal year in the outer `WHERE`, which silently dropped
  accounts whose only activity was in another year — and a trial balance still nets to zero when
  rows are missing, so the totals would not have revealed it. `ledger.numbering_gaps` relied on
  `generate_series(1, NULL)` returning nothing: the right answer for the wrong reason.
- **`ledger.*` functions must be `ALTER FUNCTION … OWNER TO ledgr_ledger`.** `SECURITY DEFINER` runs
  a function as its owner, and left owned by `ledgr_migrator` every write would fail with
  `permission denied`. It fails closed, which is the right direction, but it is the line that makes
  the whole design work; a static check asserts every `ledger.*` function is both owned and granted.
- **`period_status_transition()` stays owned by `ledgr_migrator`**, unlike the posting-table
  triggers. It guards `period`, which `ledgr_migrator` owns, and an owner that cannot drop a trigger
  on its own table buys nothing. FR-GL-007 does not ask for protection from a migration; FR-GL-003
  and CMP-009 do, and those tables are owned by `ledgr_ledger`.
- **`0020_ledger.sql` has not been executed here.** No Postgres is available in this environment, so
  it is parse-checked only: `$$` balance, every `ALTER FUNCTION`/`GRANT EXECUTE` signature resolved
  against a `CREATE FUNCTION`, every granted table known, and 105 non-plpgsql statements parsed as
  Postgres. CI applies it and runs all 91 tests for real. **Until that run is green, the guarantees
  in this ADR are designed, not demonstrated.**
- **`journal_line.cost_centre_id` exists with no feature behind it** (FR-GL-009 is `S`). Adding a
  column later to an append-only table with history is a backfill nobody can perform — there is no
  UPDATE.
- **Not built, and named rather than implied:** drafts (FR-GL-011 needs a separate mutable table —
  they cannot live in the posting tables without breaking immutability), year-end close
  (FR-GL-008), multi-currency (FR-GL-010, `S`/P3), accruals (FR-GL-012, `S`), the suppletie flow
  FR-GL-007 points at, and any HTTP surface at all. There is no ledger endpoint yet, so the
  IAM-005 isolation test and the `require_permission` wiring that every route needs are still ahead.
