# ADR-079: Batch invoicing

- **Status**: Accepted
- **Date**: 2026-09-21
- **Implements**: SI-17 (Could) - generate invoices for multiple customers/contracts in one pass
- **Builds on**: [ADR-074](ADR-074-recurring-invoices.md) (an invoice is only ever made by
  `InvoicingService`), [ADR-075](ADR-075-quotes-and-conversion.md), [ADR-078](ADR-078-sales-invoice-approval.md)
- **Constrained by**: CLAUDE.md rules 1-4, NFR-031, NFR-032, FR-AR-003/004, IAM-005

## Context

Some businesses invoice everyone at once: an annual membership fee to eighty members, a monthly consumption
statement to every contract. Doing that one invoice at a time is slow; doing it with a separate code path would
have to reimplement the statutory gate, the gapless number, the posting and now the approval gate.

## Decision

1. **A batch is a list of entries; each entry is an ordinary invoice.** Every entry goes through
   `InvoicingService.create_draft` and, if asked, `issue`, so nothing is bypassed. An entry names a customer and
   may carry its own lines, notes and payment term; whatever it omits comes from the batch's defaults, so "the
   same fee to 80 customers" is one set of lines and 80 customer ids, and "each customer's own consumption" is
   80 entries with lines. Line rules are the quote's (`validate_quote`): one definition of a usable line.
2. **Each entry has its own savepoint; a failure never spoils the rest.** An entry that cannot be raised (unknown
   or archived customer, incomplete address, a date in no fiscal year, an unusable draft) is reported with a code
   and a sentence in the reader's language, and the pass carries on. An unexpected error, including a permission
   failure, is not swallowed: it aborts and rolls back the whole request.
3. **Drafts by default; issuing is opt-in and per entry.** With `issue`, each draft is issued through the shared
   `attempt_issue` (also now used by recurring invoices): a refusal leaves the draft, burns no number and names the
   reason (`not_authorized_to_issue`, `approval_required`, `not_statutory_compliant`, `no_open_period`, ...). So a
   bookkeeper's batch under SI-16 yields drafts with `approval_required`, exactly as one invoice would. Nothing here
   sends; delivery remains its own act.
4. **A batch is a record written once.** `sales_invoice_batch` (what was asked, counts) and
   `sales_invoice_batch_item` (per entry: customer, invoice, status, error) are INSERT/SELECT only - never edited or
   deleted. An invoice belongs to at most one item. The record is written when the pass has finished, so the counts
   are known and no row needs updating.
5. **Retries are recognised by a caller-chosen `batch_key`.** A repeat returns the first batch
   (`already_exists`) and creates nothing; the unique index holds it when two arrive at once, and the loser's whole
   request - drafts included - rolls back (409, repeat it). Without a key each request is a new batch.
6. **Bounded and reviewable.** At most 200 entries; the response lists every entry. `create sales_invoice` runs a
   batch; the route also requires a verified e-mail (with `issue` it posts to the ledger, IAM-010b).
7. **Audit records counts, not customers.**

## Consequences / out of scope

- **No selection language** ("all customers with tag X"): the caller sends the customer ids. Recurring schedules
  (SI-07) already cover "everything due", so a batch from schedules is not duplicated here.
- **Not asynchronous.** A pass of 200 entries runs inside one request; it is bounded for that reason. A background
  job would need the scheduler that does not exist yet.
- **No batch-level undo.** Discarding a draft is the existing per-invoice action; an issued invoice is credited.
- **Duplicate warnings (SI-12) are not applied** inside a pass: two entries for the same customer are two invoices.
- Migration 0061 is additive (two new tables).

## Found while building it

`RecurringInvoiceService._try_issue` did not know the approval gate (ADR-078): with approval required, a schedule
with `auto_issue` would have raised `ApprovalRequired` and aborted the whole run instead of leaving a draft. Its
issue attempt now lives in `api.invoicing.issue_attempt` and maps that refusal to `approval_required`; recurring
and batch invoicing share it.