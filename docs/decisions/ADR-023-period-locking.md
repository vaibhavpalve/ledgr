# ADR-023: Period locking, the unlock authority, and the suppletie flow

- **Status**: Accepted
- **Date**: 2026-09-01
- **Implements**: FR-GL-007 (PRD §6.2). Ledger side of FR-VAT-005.
- **Builds on**: [ADR-022](ADR-022-ledger-bounded-context.md).

## Context

> **FR-GL-007.** Period locking per fiscal period with a defined unlock authority; VAT-filed periods
> are hard-locked and require a suppletie flow to change.

Migration 0020 already refused postings into a non-open period. What it had no notion of was an
**authority**: any code holding a database connection could `UPDATE period SET status = 'open'` and
then post freely. The lock was a data value, not a control.

Two questions to answer, and they have different kinds of answer. *Who* may unlock is an
authorization question. *What stops the check being skipped* is a privilege question.

## Decision

### The authority is Appendix A's, not a new one

Nothing here invents an authority. Appendix A's row "Lock / unlock periods" is Full for **Owner**
and **Accountant** and absent for everyone else, and PRD §8.4 lists "Unlock closed periods" among
the things a Bookkeeper explicitly *cannot* do. That is `("lock", "period")`, already in
`api.authz.matrix`, and `PeriodService` evaluates it through the one shared library (CLAUDE.md
rule 3), per call, against live state.

Three permissions, deliberately not one:

| Operation | Permission | Roles |
|---|---|---|
| lock / unlock | `lock period` | Owner, Accountant |
| mark filed (applies the hard lock) | `file vat_return` | Owner, Accountant |
| open a suppletie | `prepare vat_return` | Owner, Accountant, **Bookkeeper** |
| close a suppletie | `file vat_return` | Owner, Accountant |

The Bookkeeper sits in the gap on purpose. Appendix A grades "Prepare VAT return" and "File VAT
return" differently, so a Bookkeeper may prepare a correction and may not file it. Had open and
close both checked the filing permission, that Appendix A grant would be decorative.

Locking and filing hold the same roles *today*. They are still separate checks, because they are
separate Appendix A rows and a future change to one must not silently move the other.

### What makes the check unskippable is a privilege, not a convention

`period_status_transition()` refuses any status change unless `current_user` is `ledgr_ledger`.
Inside a `SECURITY DEFINER` function `current_user` is the function's *owner*, so that is true
exactly when the change arrived through `ledger.lock_period`, `ledger.unlock_period` or
`ledger.mark_period_filed` — each of which is called only after `PeriodService` has checked the
permission. A direct `UPDATE` from application code is rejected.

This is the same shape ADR-022 used for postings, narrowed to one column. The rest of the `period`
row stays writable as 0001 granted it, because only `status` is what FR-GL-007 governs — a guard
that refused every update would break ordinary maintenance and would be a wider change than the
requirement asks for.

There is deliberately **no superuser exemption**. One would buy nothing: a superuser can drop the
trigger outright, so an exemption would only widen the path for roles that cannot.

### The hard lock is terminal, and the route out is a different filing

`vat_filed` has no outgoing transition. Not for the Owner, not through the API, not with a flag.
Reopening a filed period would let the ledger and a return already sent to the Belastingdienst
diverge with nothing recording that they had.

So "requires a suppletie flow to change" is implemented as: **the change happens somewhere else.**
In Dutch practice the original aangifte is never amended; a separate correction return states the
difference. Concretely:

- `ledger.open_suppletie()` opens a `vat_suppletie` against a **filed** period
- corrections are posted into whichever period is **open**, never into the filed one
- those entries carry `suppletie_id`, which is FR-VAT-005's "clear link to the original filing"

`journal_entry_validate()` refuses a correction whose period *is* the period its suppletie corrects
— the mistake that would make the hard lock meaningless.

At most one suppletie is open against a period at a time (partial unique index). Two concurrent
corrections would make "the net difference to report" ambiguous, and being unambiguous is the whole
value of the link. Closed ones are unconstrained: a period may genuinely need correcting more than
once.

**What is built is the ledger side.** The return itself — rubriek values, the Digipoort submission,
the receipt — is FR-VAT-005 and is not built.

### `HardLocked` is not a permission error

Its own exception type, and checked *before* the permission check. A user hitting a filed period
should be told to open a suppletie, not that their role is insufficient — because no role is
sufficient, and reporting it as a denial sends them looking for someone with a bigger role who does
not exist.

### Who unlocked is an audit entry, not a column

Unlocking clears `locked_at` and `locked_by_user_id`, so the attribution lives in the audit log —
append-only and hash-chained (0019), which is a stronger record than a column any later `UPDATE`
could overwrite, and unlike a column it survives the next lock/unlock cycle instead of being
replaced by it. The service captures `previously_locked_by` into the entry *before* the transition
clears it, or it would be lost.

`unlock()` requires a stated `reason`. Reopening a closed period is the most consequential thing
this service does — figures someone has already relied on become editable — and it is the operation
an inspector asks about.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Check the permission in the service; leave `period` fully writable | Advisory. Any code with a session could reopen a period and post into it, and the authority would exist only in the code path that happened to call it. |
| A separate `period_lock_event` history table | Duplicates the audit log, which already records this and is hash-chained. Two sources of truth that can disagree, for a query the audit log answers. |
| Allow `vat_filed → locked` as an "administrative correction" | It is exactly the reopening the requirement forbids, wearing a different name. |
| One `manage period` permission covering lock, file and suppletie | Collapses three Appendix A rows into one and erases the Bookkeeper's prepare-but-not-file position. |
| Store the suppletie's corrections as a period-level adjustment | Would mean writing to a hard-locked period. The corrections are ordinary postings in an open period; only the *link* is special. |
| Move `period` ownership to `ledgr_ledger` entirely | Wider than the requirement, and would disturb 0001's grants and tests for no gain — only `status` needs the gate. |
| `mark_filed` audited as `configuration` | It is the ledger half of a statutory filing and is what applies the hard lock. An inspector looks for it under `filing`. |

## Consequences

- **68 DB-free tests and 17 DB-gated ones.** The pure suite drives **every** standard role at each
  operation rather than the two that should pass: a permission bug does not usually take an
  authority away, it hands one out, and only an exhaustive check sees that.
- **The expectation is read out of the matrix, and the matrix is pinned against Appendix A
  separately.** Two distinct failures: a service that ignores a permission, and a matrix that has
  drifted from the PRD.
- **15 guards mutation-tested, 13 caught individually.** The two survivors — "a suppletie can be
  opened against any period" and "vat_filed has an outgoing transition" — survive because each
  guard exists at *two* layers and the other caught it. Removing **both** layers is caught in each
  case, which was verified rather than assumed. That is defence in depth working, not a test gap;
  the load-bearing copy is the one in SQL.
- **`AuditCategory.FILING` now has a producer**, and `tests/audit/test_wiring.py` names it
  precisely: `api.ledger.periods`, the ledger half only. The registry entry says so, because
  "filing is wired" would otherwise read as though FR-VAT were built.
- **`ledger.post_entry` gained a twelfth parameter and had to be DROPped and recreated.** Postgres
  identifies a function by its argument types, so `CREATE OR REPLACE` with an added parameter
  produces a second *overload* and a caller passing eleven arguments keeps reaching the old body.
  The static checker now flags a function that is both dropped and created at the same signature,
  and flags any `ledger.*` function that is not re-owned after a drop — a recreated function belongs
  to the migration role again, and `SECURITY DEFINER` would then run it with privileges that cannot
  write a posting.
- **Two existing integration tests had to change**, and that is the change landing: they moved
  `period.status` with a direct `UPDATE`, which the new trigger refuses. They now go through
  `ledger.unlock_period`.
- **`period.filed_at`, `filed_by_user_id` and `filing_reference` exist with no filing flow behind
  them.** Added now because a filed period cannot be un-filed, so a later backfill would have no way
  to reach the rows that need it.
- **`0021_period_locking.sql` has not been executed here.** Parse-checked only — `$$` balance, every
  `ALTER FUNCTION`/`GRANT EXECUTE` signature resolved, 46 statements parsed. CI applies it and runs
  the 17 DB-gated tests for real. **Until that run is green, the privilege gate is designed, not
  demonstrated** — and it is the gate that makes the authority more than a convention.
- **Not built:** FR-VAT-005's correction return, FR-GL-008's year-end close (`fiscal_year.status`
  is checked but nothing sets it), and any HTTP surface — `PeriodService` has no route, so the
  IAM-005 isolation test and `require_permission` wiring every endpoint needs are still ahead.
