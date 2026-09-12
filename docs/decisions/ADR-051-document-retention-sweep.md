# ADR-051: The document retention sweep

- **Status**: Accepted
- **Date**: 2026-09-13
- **Implements**: PRIV-030 (PRD §10.4)
- **Related**: [ADR-025](ADR-025-ledger-integrity-job.md) - the job shape this reuses;
  [ADR-030](ADR-030-document-storage.md) - the retention and restriction design this enforces;
  [ADR-052](ADR-052-data-subject-erasure.md) - the erasure flow that puts documents into `restricted`

## Context

> **PRIV-030.** Retention is enforced by automated jobs, not manual process, with an exception
> report for records that failed to expire.

Migration 0031 already makes removal safe: `document_deletion_guard` permits an unconditional
`DELETE` once `retention_until < current_date`, whatever the row's `status` - a `restricted`
document (PRIV-023) is swept exactly like an `active` one, no approval needed for either. What did
not exist was the job that finds those rows and runs it, or the exception report PRIV-030 names.

The gap was already flagged in the schema. `document_retention_idx`'s comment says "PRIV-030's
expiry job sweeps by date across tenants", and `api.documents.service.expired_documents` is
labelled "PRIV-030's sweep, as a pure function over rows" - built, and never wired to anything that
deletes.

## Decision

### The selection is SQL, `documents.expired()` (migration 0045)

The same division `api.ledger.integrity` documents for 0023's checks: the comparison has to be
made against the rows, `SECURITY INVOKER` (the default, stated explicitly) is what lets `ledgr_app`
see one tenant and `ledgr_ops` (`BYPASSRLS`) see every one with no per-tenant loop, and a `SECURITY
DEFINER` version would report a clean archive for every tenant through a role with no `BYPASSRLS` -
ADR-025's exact failure, one schema over.

### The exception is decided in advance, not caught

A document ever linked to a posting can never actually be deleted: `document_posting_link` is
immutable and never removed (FR-DOC-003, migration 0031), and its foreign key to `document` has no
`ON DELETE` action. A `DELETE` against such a row raises a foreign key violation - which says only
that *something* referenced the row, not what or why.

`documents.expired()` returns `blocking_link_count` - the count of `document_posting_link` rows,
live or detached, naming the document - so `DocumentRetentionSweepJob` can skip the `DELETE`
entirely and report the reason in FR-DOC-003's own terms:

    retention expired on 2020-06-30 but 2 document_posting_link row(s) still name
    this document (FR-DOC-003); those links are never deleted, so this document
    cannot be removed while they exist

That sentence is PRIV-030's "exception report for records that failed to expire" - not a crash,
and not a silent skip.

### One report, one alert, the same shape as the ledger integrity job

`RetentionSweepReport` (examined, deleted, exceptions) and `LoggingRetentionSweepAlerter`, called
once per run and only when there is an exception - NFR-045's "alerts that cannot be acted on are
deleted rather than tolerated" applies here exactly as it does to `api.ledger.integrity`: a
thousand blocked documents from one systemic cause is one incident, not a thousand pages.

Unlike the ledger integrity job, there is no separate "examined nothing" exit code. An empty
`documents.expired()` result is the ordinary, expected state of a healthy archive on most nights -
not the ambiguous case ADR-025 guards against, where zero findings could mean "clean" or "the
connection could not see anything". `report.examined == 0` and `report.clean` are simply both true.

### The same command nightly and on demand

`scripts/enforce_document_retention.py`, `make enforce-document-retention` - mirroring
`scripts/verify_ledger_integrity.py`'s shape (`--administration`, `--app-connection` +
`--organization`, `--json`) so a developer already knows how to run it. Exit codes:

    0  clean - every document past retention was removed, or none were due
    1  at least one exception
    2  could not run

### Deletions commit; the ledger integrity job's script does not

`verify_ledger_integrity.py` is read-only and never commits. This job writes, so
`scripts/enforce_document_retention.py` commits once per run, after the whole sweep - a genuine
failure rolls every tenant's deletions in that run back together rather than leaving some tenants
swept and others not, which matters more here than a per-document commit would help, since every
`DELETE` sent has already passed the `blocking_link_count` check and is not expected to fail.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Catch the foreign key violation instead of predicting it | Reports only that something referenced the row, not what (FR-DOC-003) or how many - the opposite of an actionable exception report |
| `SECURITY DEFINER` on `documents.expired()`, consistent with 0031's write paths | Those functions write and need `ledgr_ledger`'s only `INSERT`; this only reads, and a definer version would hide the sweep's cross-tenant scope behind a role with no `BYPASSRLS` - ADR-025's failure |
| Delete `document_posting_link` rows for an expired document first, then the document | Contradicts FR-DOC-003 and migration 0031's own immutability trigger on the link table - the link is detached, never deleted, and this job does not get an exception to that rule |
| An "examined nothing" exit code, matching the ledger integrity job | That ambiguity exists because a healthy ledger with zero findings looks identical to an unreadable one. An empty document sweep has no such twin state - `documents.expired()` returning nothing IS the healthy state |
| Per-document commits | Leaves a sweep half-applied across tenants on a mid-run failure, for no gain: every candidate DELETE has already been screened by `blocking_link_count` and is not expected to fail |

## Consequences

**Easier.** PRIV-030 is enforced the way NFR-033 already is for the ledger: one job, one report, a
Makefile target, an exit code a CI gate or an operator can act on.

**Still to do.** No runbook per exception reason yet (there is currently exactly one:
`document_posting_link` still exists). PRIV-032's legal hold, once built, will need its own
exclusion in `documents.expired()` - a held document must not appear as a candidate at all, the
same way `document_posting_link` already keeps one out by blocking the delete rather than by never
selecting it. That distinction (blocked vs. never a candidate) is worth keeping when PRIV-032 is
implemented, because a legal hold is a decision someone made, not a structural fact about the
schema.

**Not built.** Retention sweeps for the other categories PRD §10.4 lists - audit log, bank
transaction data, PSD2 consent, billing records, support conversations, product analytics - are
deliberately out of scope here. `document` is the only one of them modelled in this codebase today
with both a real expiry rule and a schema that permits deletion; migration 0019 is explicit that
the audit log's own retention is satisfied by never deleting at all, and any purge that becomes
necessary there "must not be a DELETE ... which is a schema change and a new ADR, not a cron job" -
the same stance this ADR takes toward any category that does not yet have 0031's deletion guard.
