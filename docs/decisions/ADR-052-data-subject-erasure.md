# ADR-052: Data subject erasure - honour, or restrict and explain

- **Status**: Accepted
- **Date**: 2026-09-13
- **Implements**: PRIV-021, PRIV-022, PRIV-023 (PRD §10.3)
- **Related**: [ADR-030](ADR-030-document-storage.md) - PRIV-023's `restricted` status, already
  modelled there; [ADR-038](ADR-038-customer-master.md) - the customer/invoice split this decision
  turns on; [ADR-051](ADR-051-document-retention-sweep.md) - what actually removes a `restricted`
  document once its retention ends

## Context

> **PRIV-022.** Erasure requests are honoured except where fiscal retention law requires
> preservation. The system distinguishes the two and gives a clear, specific explanation of what
> was retained and why.
>
> **PRIV-023.** Where erasure is blocked by retention law, the record is restricted: access limited
> to fiscal purposes, excluded from analytics and search, deleted automatically at the end of the
> retention period.

Two resources in this codebase carry personal data and sit inside a fiscal retention rule: the
document archive (FR-DOC-002, seven or ten years from the end of the fiscal year) and the customer
master (CMP-001, via the invoices raised against it). Migration 0031 had already modelled half of
PRIV-023 - `document.status` has carried a `restricted` value since the document archive was built,
with `DocumentStatus.RESTRICTED`'s docstring naming PRIV-023 directly - but nothing set it, and
nothing decided when to.

## Decision

### Two resources, two different answers, because they are different facts

**A document is (almost) always restricted, never erased outright, while it is live.** FR-DOC-002's
retention window covers the *whole* document for its *whole* life; there is no sense in which part
of a live document is unprotected. So `DocumentService.request_erasure` restricts, with one
exception: a document already past its `retention_until` needs no restriction at all - `outcome`
is `erased`, unaccompanied by a synchronous delete (see below), because the next PRIV-030 sweep is
what actually removes it.

**A customer master row is always erased outright; what varies is its invoices.** 0039's header is
explicit that an invoice snapshots what it needs onto its own columns and never reads back through
the customer row - so the master record is never itself the artifact CMP-001 protects, and
anonymising it is always safe. What CMP-001 *does* protect is an **issued invoice's own frozen
snapshot** (`customer_name`, `customer_address`, `customer_country`, `customer_vat_number` on
`sales_invoice`), which 0037/0039's freeze trigger already forbids changing. So
`CustomerService.request_erasure` anonymises the master row unconditionally, then checks whether
any issued invoice's snapshot is still inside its own seven-year window; if so, the *overall*
decision is `restricted`, and `explanation` names each one, by reference, date, and the date its
retention ends.

Both services return one shared shape, `api.privacy.model.ErasureDecision` - not because the rule
is shared (it is not: one is a fact about a document's own life, the other about invoices that
outlive the master record naming them), but because a client reading either response should learn
the same fields the same way.

### The explanation is data, not only prose

`retained_until` is what a caller can check without parsing a sentence; `explanation` is what a
person reads, and it always names the specific rule, record, and date - never "retention law
applies". `data_subject_erasure_request` (migration 0044) stores it too, insert-only, so the answer
to "what happened to my erasure request" outlives whatever call produced it - the same reasoning
0031's `document_deletion_request` already rests on for FR-DOC-005.

### Neither permission is invented

`DocumentService.request_erasure` requires `upload document` - the permission `link_to_posting`
already reuses for a different mutating, evidentiary act on a document, and this is at least as
privileged. `CustomerService.request_erasure` requires `manage customer`, the one permission 0039
already governs every write behind. ADR-012's rule against inventing permissions is satisfied
without touching `api.authz.matrix` or PRD Appendix A.

### Erasure has no restore

`CustomerIsArchived` has a restore call; `CustomerIsErased` does not, and there is no code path that
clears `erased_at` once set. `customer_erasure_is_frozen_trg` (migration 0044) enforces this at the
database layer too, the same shape `document_original_immutable` gives the archive's original -
an application bug that called the ordinary update path against an erased row must not be able to
reinstate what erasure cleared.

### A document's erasure request never deletes synchronously

`DocumentService` runs as `ledgr_app`, which migration 0031 deliberately grants no `DELETE` on
`document` at all - `documents.delete_with_approval`'s own comment says why: "a request handler
must not be able to delete a document even with an approval row in front of it". A document already
past retention when an erasure request arrives is therefore reported as `erased` without a
synchronous delete; ADR-051's nightly sweep is what actually removes it, on its own privileged
connection.

## Alternatives considered

| Option | Rejected because |
|---|---|
| One shared "erasure rule" module deciding for both resources | The rules are genuinely different facts (a document's own life vs. invoices that outlive their customer row) - forcing one shared decision function to explain both would fit neither, and the codebase's own precedent (`api.documents.retention` vs. `api.ledger`'s fiscal-year handling) is separate rules per bounded context |
| Deleting the customer row and re-creating a "deleted customer" placeholder on invoices | 0039 grants no `DELETE` on `customer` at all, for referential reasons unrelated to GDPR - every invoice ever raised still points at the row. Working around that grant would undo a decision 0039 already made deliberately |
| A `restricted` status column on `sales_invoice` | The frozen snapshot columns cannot be touched either way (0037/0039's freeze trigger), and there is nothing else to gate: no invoice search or analytics pipeline exists yet to exclude from. The restriction is recorded in `data_subject_erasure_request` instead, and the exclusion is a documented obligation for whichever pipeline is built first |
| A new `privacy` audit category | `api.audit.log.AuditCategory`'s docstring calls itself closed and mirrored to IAM-090's exact clauses - one that IAM-090 does not name is "scope creep" by its own stated rule. Erasure events use `CONFIGURATION`, the same category customer and document administrative actions already use |
| Letting `DocumentService.request_erasure` delete an already-expired document synchronously | Would need a `DELETE` grant on `document` for `ledgr_app` that migration 0031 deliberately withholds from every request handler, for exactly this reason |

## Consequences

**Easier.** PRIV-021's "tooling" now exists as two ordinary, authorized, audited API calls - `POST
.../documents/{id}/erasure-request` and `POST .../customers/{id}/erasure-request` - each returning
a decision a support agent or an automated DSR workflow can act on directly.

**Harder.** A customer's erasure is genuinely partial when invoices are outstanding, and that has
to be communicated rather than hidden - `explanation` is the whole point of PRIV-022's wording, and
a generic "request received" would fail it.

**Not built.** No analytics pipeline exists yet to exclude a restricted invoice or an erased
customer from - `explanation` says so explicitly rather than implying an exclusion that is not yet
enforced anywhere. Automatic deletion of a restricted invoice at the end of its own retention
period is also not built: `sales_invoice`'s issued-row delete trigger currently forbids deletion
unconditionally, with no time-based exception the way `document_deletion_guard` has one. Extending
it is a deliberate schema change with its own blast radius on CMP-009's append-only guarantee, and
does not belong in this change - see ADR-051's own "not built" for the parallel reasoning about the
audit log.
