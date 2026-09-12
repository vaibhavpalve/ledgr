# ADR-025: The ledger integrity job

- **Status**: Accepted
- **Date**: 2026-09-01
- **Implements**: NFR-033 (PRD §12.4)
- **Verifies**: FR-GL-001, FR-GL-006, FR-GL-013, CMP-009
- **Related**: [ADR-022](ADR-022-ledger-bounded-context.md) — the invariants this checks;
  [ADR-020](ADR-020-audit-log.md) — the same prevention/detection argument, for the audit log

## Context

> **NFR-033.** A nightly integrity job verifies debit/credit balance, sub-ledger to control
> account agreement, and numbering continuity, alerting on any deviation.

All three are already enforced at write time by migration 0020: a deferred constraint trigger for
balance, a `BEFORE INSERT` trigger for the control-account biconditional, and an allocator row
behind a `UNIQUE` index for numbering. On a correct database this job finds nothing, on every
tenant, forever.

That is the question the requirement answers, and 0020 already answered it once for one of the
three checks, in the comment on `ledger.numbering_gaps()`:

> The allocator makes gaps impossible, so this report should never return a row. It is built anyway
> because a report that can only ever say "no gaps" is the check ON the allocator.

The write-time guards bind every writer, and they bind them only at write time. They do not cover a
superuser who runs `ALTER TABLE journal_line DISABLE TRIGGER ALL` (ADR-020 concedes the same limit
for the audit log, and `tests/integration/test_audit_tamper_evidence.py` demonstrates it), a restore
of a backup taken mid-transaction, a replication failover that lost the tail of a series, a data
migration between databases, or a future migration that drops a guard. In each of those the books
are wrong and nothing has raised.

CMP-009 also asks for gaplessness "sufficient to satisfy inspection standards". An inspector asks
to see the report, not the trigger.

## Decision

### The checks are SQL, in migration 0023

`ledger.integrity_balance()`, `ledger.integrity_control_accounts()`, `ledger.integrity_numbering()`,
unioned by `ledger.integrity_findings()`. Python receives rows and turns them into a report; it
computes nothing.

Re-deriving a balance in Python from rows Python has already read tests the reader, not the books.
One statement also sees one snapshot, where three round trips would let a posting commit between
checks and produce a report describing no state the database was ever in.

### `SECURITY INVOKER`, unlike every other function in the `ledger` schema

0020's functions are `SECURITY DEFINER` because they **write**, and `ledgr_ledger` holds the only
`INSERT` privilege in the system. These only read, and both callers already hold `SELECT`. Running
them as the invoker is what makes both callers correct:

| Caller | Sees | Because |
|---|---|---|
| `ledgr_app` | one tenant | RLS, the same first line of defence as every other query path |
| `ledgr_ops` | every tenant | `BYPASSRLS` (0002), so the nightly sweep needs no per-org loop |

`SECURITY DEFINER` would have broken the second silently: `ledgr_ledger` is `NOBYPASSRLS` and these
tables are `FORCE ROW LEVEL SECURITY`, so an ops sweep through a definer function would have
reported a clean bill of health for every tenant in the system. That is the worst failure available
to a check like this, and it would have looked exactly like success.

### Owned by `ledgr_ledger` anyway

Ownership does not affect how an invoker function runs. It decides who may replace it. Left owned by
`ledgr_migrator` — which owns most of this schema — a migration could swap any check for one that
returns nothing, and every test in the suite would still pass. 0019 owns `app.verify_audit_chain()`
with `ledgr_audit` for the same reason, and
`tests/integration/test_audit_tamper_evidence.py` enumerates "replace the verifier so tampering
looks clean" as an attack. `tests/integration/test_ledger_integrity.py` asserts the equivalent here.

### The scope travels with the report, and an empty scope is not a pass

Every check reports a problem by **returning a row**, so "no findings" means both "clean" and
"looked at nothing", and a caller counting findings cannot tell them apart. A run as `ledgr_app`
with no tenant context set is exactly that case: RLS fails closed, zero rows come back, and the job
would certify a database it could not read.

So `ledger.integrity_scope()` returns what the caller could see, `IntegrityReport.examined_nothing`
exposes it, `raise_for_deviations()` refuses an empty scope by default, and the CLI has a **separate
exit code** for it:

    0  intact, over a scope that contained something
    1  deviations found
    2  could not run
    3  nothing examined

### The allocator is the numbering series' anchor

`journal_sequence.next_number` is checked against `max(entry_number) + 1`. This is the check that
catches a deleted **tail**, which gap detection structurally cannot: delete entries 4 and 5 and the
remaining 1..3 is perfectly gapless. The allocator is the only record that a fourth number was
issued — the same role an external anchor plays for the audit chain in ADR-020, kept where the
deletion did not reach. `series_missing` is its mirror: numbers issued, no entries at all.

Numbering findings are reported **per series**, with a count and a capped sample, not per missing
number. A series with a thousand holes is one incident, and NFR-045 says alerts that cannot be acted
on are deleted rather than tolerated.

### Fourteen deviation keys, and a corruption case for each

`entry_unbalanced`, `entry_without_lines`, `entry_with_one_line`, `orphan_line`,
`administration_unbalanced`, `control_line_without_party`, `party_line_off_control`,
`party_kind_mismatch`, `party_from_other_administration`, `control_subledger_difference`,
`numbering_gap`, `sequence_drift`, `sequence_missing`, `series_missing`.

The key is the runbook key: `entry_unbalanced` and `orphan_line` are both FR-GL-001 failures and
have nothing in common as an incident.

A check that has only ever run against healthy books is indistinguishable from `WHERE false`. So
`tests/ledger/integrity_cases.py` breaks the ledger in thirteen specific ways and names what must
come back; `tests/ledger/test_integrity.py` runs that table against the in-memory ledger on every
`make test-api`, and `tests/integration/test_ledger_integrity.py` runs the same table against
Postgres with the append-only triggers disarmed. `test_every_deviation_the_job_can_report_has_a_case`
fails the build if a check is added without one.

### One alert per run, and no audit entry

`IntegrityAlerter.deviations_detected()` is called once, with the whole report, only when something
was found. A job that pages on every healthy night is one whose pages are muted by the second week.

No audit entry is written, and that is a constraint rather than an oversight. The sweep runs as
`ledgr_ops`, which 0019 gave `SELECT` on `audit_log` and no `INSERT`: "a role that could write the
log recording its own access is not an auditor of itself". Writing one would need a privilege that
undoes that, to record something no tenant did.

### The same command nightly and on demand

`scripts/verify_ledger_integrity.py`, and `make verify-ledger-integrity`. The scheduled path is the
one a developer already runs by hand, which is what stops it being a path nobody has watched
execute. The exit codes above are the interface, which is what makes it usable as a test oracle:
post whatever a scenario posts, then ask whether the books still hold.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Compute the checks in Python from rows the job reads | Tests the reader rather than the books, needs three snapshots, and requires no privilege the roles do not already have as SQL |
| `SECURITY DEFINER`, consistent with the rest of the schema | `ledgr_ledger` is `NOBYPASSRLS` under `FORCE ROW LEVEL SECURITY`, so the cross-tenant sweep would report every tenant clean — a silent, total false negative |
| Store sub-ledger balances and reconcile them to the control account | A second set of numbers is precisely what creates the drift FR-GL-006 avoids by construction. The check is aimed at the construction, not at a difference between two ledgers |
| Reuse `ledger.numbering_gaps()` per journal from the job | Cannot see a deleted tail, and emits one row per missing number — a gap-riddled series becomes a thousand alerts |
| One alert per deviation | Same problem, one layer up; NFR-045 |
| Write an audit entry per run | `ledgr_ops` deliberately holds no `INSERT` on `audit_log` (0019). Granting one to record a read would weaken IAM-090 |
| Exit 0 when the sweep saw nothing | Makes the oracle certify databases it never read — the failure this design is most concerned with |
| Fail fast on the first deviation | A partial report is worse to be paged with than a complete one; the run is read-only and cheap enough to finish |

## Consequences

**Easier.** Any test can assert "and the books still hold" in one call, against either the fake or a
real database. Any deploy or migration can be gated on `make verify-ledger-integrity`. An inspector
can be shown a report rather than a trigger definition (CMP-009).

**Still to do.** NFR-045 requires a runbook per alert; there are fourteen deviation keys and no
runbook entries yet. `LoggingIntegrityAlerter` writes structured logs, which a log-based alert rule
can fire on today, but routing them to a pager is deployment work — the seam is `IntegrityAlerter`,
and swapping the implementation is the whole change. This mirrors where
`scripts/anchor_audit_chain.py` deliberately stopped.

**Cost.** The sweep is full-table aggregates over `journal_entry` and `journal_line` per
administration. Nightly and read-only, so a read replica is a natural home for it later; nothing in
the design assumes the primary. The one enumeration that could be expensive — `generate_series` over
a numbering series — is guarded by `entries <> highest`, which is the gaplessness invariant itself
(unique numbers ≥ 1 mean `count = max` iff the series is exactly `1..max`), so healthy series are
rejected by comparing two aggregates and never expanded.

**A note on `to_char`.** `ledger.money_text()` uses `FM…0.00` rather than 0020's `FM…0D00`. `D` is
the *locale's* decimal separator: under `lc_numeric = nl_NL` it renders `0,01`, which any consumer
parsing the report as a number would read as zero or reject. `.` is locale-independent. The same
pattern appears in `ledger.reverse_entry()`, where the value is cast straight back to `numeric` — so
a non-C `lc_numeric` would make reversals fail loudly rather than silently, and it is a latent
fragility rather than a live bug. Worth fixing in a future migration; not fixed here, because
rewriting a shipped function is a change with its own blast radius and does not belong in this one.

**Not built.** No HTTP endpoint. When "check my books" becomes a feature, it should construct
`LedgerIntegrityJob` over the request's `ledgr_app` session — the job takes a repository protocol
for exactly that reason — and the authorization decision goes through the single shared library
like every other (CLAUDE.md rule 3).
