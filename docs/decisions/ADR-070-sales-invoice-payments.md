# ADR-070: Payments received against a sales invoice

- **Status**: Accepted
- **Date**: 2026-09-19
- **Implements**: the prerequisite of SI-04 (FR-AR-010) and SI-06 (FR-AR-012): an invoice has an
  outstanding balance. Touches FR-AR-009's "incoming payment matches the invoice" only as far as
  *manual* recording; automatic matching is FR-BNK-003
- **Constrained by**: CLAUDE.md rules 1 and 2 (tenancy, append-only ledger), rule 3 (one
  authorization library), rule 4 / NFR-031 (no floats), NFR-032 (idempotency), FR-GL-003
  (corrections are reversals), FR-GL-006 (receivable is a control account with a sub-ledger)
- **Related**: [ADR-039](ADR-039-invoice-posting.md) - the invoice's own entry this settles;
  [ADR-034](ADR-034-duplicate-detection.md)'s "warn, never block" does *not* apply here, see §5

## Context

SI-04 asks which invoices are overdue, and overdue means *issued, past its due date and not paid*.
Nothing in the schema could say an invoice was paid: migration 0037 stops at issue, 0040 posts the
receivable, and bank reconciliation (FR-BNK-*) is not built. A reminder ladder on that base would
chase customers who had already paid. So the receivable gets its other half first.

The choice was between (a) a payment table with no ledger entry, faster but leaving the books and
the invoice disagreeing until a follow-up posts them, and (b) recording payments through the ledger.
This ADR records (b), chosen by the product owner on 2026-09-19.

## Decision

### 1. A payment is a row *and* a ledger entry, written together

```
Dr  Bank / Kas (an asset account)    amount
Cr  Debiteuren (AR control)          amount   <- carries the customer's sub-ledger party
```

`SalesPaymentService` builds an `EntryInput` and hands it to `LedgerService.post`; nothing in the
invoicing package touches `journal_entry` or `journal_line` (rule 1, enforced by
`tests/ledger/test_bounded_context.py`). `sales_invoice_payment.journal_entry_id` is `NOT NULL` and
unique: a payment with no entry would be a receipt the books do not know about, the mirror of the
gap 0040 closed for issue.

Because the receipt credits the *same* control account and party as the invoice's own entry, the
invoice's outstanding balance and the debtor's sub-ledger balance fall by the same amount. There is
no second balance to drift; an integration test asserts they agree.

The bank account is chosen **per payment**, not mapped once: an administration commonly has several,
and only the person recording the money knows which received it. The database refuses an account
that is not this administration's, not an `asset`, or a control account (debiting Debiteuren against
Debiteuren would settle nothing). The journal is the administration's one active `bank` journal, or
`cash` for a cash payment.

### 2. "Outstanding" has one definition

`invoicing.invoice_balances(administration_id, invoice_id?)`:

```
outstanding = gross  -  credited  -  paid
```

`gross` is the frozen VAT totals (0037); `credited` the gross of the invoice's issued credit note;
`paid` the unvoided payments. The overpayment guard reads it, the Python repository reads it, and
dunning (SI-04) and aged receivables (SI-06) will read it. A second implementation is how a reminder
and a statement come to disagree about what somebody owes, so there is not one.

### 3. Immutable, with one exception: a payment can be voided, once

A mistaken payment is corrected the way the ledger corrects everything - a **reversing entry**
(FR-GL-003) - never by edit or delete. The row records it: `voided_at`, `voided_by_user_id` and
`void_journal_entry_id` are set together (a CHECK), once, and nothing else about the row may change.
`ledgr_app` has no `DELETE` grant. A voided payment stops counting toward `paid`, so the invoice is
owed again, and stays in the list as history.

The reversal is dated the day it is made, not the payment's date: the payment's period is usually
closed by the time the mistake is found, and the correction belongs in the open period.

### 4. Separation of duties: `post journal_entry`, not `send sales_invoice`

Recording needs `post journal_entry`, voiding needs `reverse journal_entry` - Appendix A's own rows
(ADR-012, no invented permissions). This is deliberate. The Invoicer holds `send sales_invoice` and
may not post, so the person who raises an invoice cannot also be the person who records that cash
arrived against it. Letting them would hand out the classic lapping opportunity: take a payment,
never record it, and nothing anywhere disagrees. `create sales_invoice` (already held by anyone who
can work on the invoice) is enough to *read* the balance and payments.

### 5. Overpayment is refused, not parked - and this one *does* block

ADR-034's "warn, never block" is right for a duplicate, where a legitimate repeat exists. It is wrong
here: a payment above the outstanding balance has no legitimate reading until allocation exists.
Silently absorbing the excess into an account nobody reads hides somebody's money. So it is refused
with a message naming the outstanding amount, and split allocation and overpayment handling are left
to FR-BNK-005 with bank reconciliation.

The refusal is checked in the service for a clear error and **again by `sales_invoice_payment_guard`
under a `FOR UPDATE` lock on the invoice row**, which is what actually holds when two payments arrive
at once. The repository translates that trigger's error into the same refusal, so the race answers
like the ordinary case rather than as a 500.

### 6. Endpoints and idempotency

```
POST .../sales-invoices/{id}/payments                        record
GET  .../sales-invoices/{id}/payments                        list + balance
POST .../sales-invoices/{id}/payments/{payment_id}/void      void
```

Amounts are strings on the wire and `Decimal` throughout; a float, a non-positive amount or one with
more than two decimals is refused rather than rounded - a payment is a fact about money that moved.
The ledger entry is idempotent on the payment's own id (`sales_invoice_payment:{id}`, and
`..._void:{id}` for the reversal), and the mutating routes sit behind the existing idempotency
middleware and `require_verified_email` (IAM-010b, as for issue). Each has a tenant-isolation test
(IAM-005).

## Alternatives considered

| Option | Rejected because |
|---|---|
| A payment table with no ledger entry | Leaves the invoice saying "paid" while the debtor's balance says otherwise, until a follow-up posts them. |
| Mark an invoice `paid` with a flag | Loses partial payment, loses the amount and date, and cannot be voided by a reversal. |
| Let the Invoicer record payments (`send sales_invoice`) | Removes the separation between raising an invoice and recording the cash against it. |
| Map one bank account per administration | Wrong for anyone with two accounts; the recorder knows which one received the money. |
| Park overpayments on account | Needs an unallocated-receipts concept and reconciliation to be safe; deferred to FR-BNK-005. |
| Recompute outstanding in Python and in the trigger | Two definitions that can disagree about what a customer owes. |

## Consequences

**Easier.** SI-04 (dunning), SI-05 (payment links + auto-match) and SI-06 (aged receivables) all sit
on `invoicing.invoice_balances`. A wrongly recorded payment is undone in one call with a full audit
trail.

**Harder.** A bookkeeper must pick a bank account per payment. There is no bulk import: many
payments at once is bank-statement import (FR-BNK-002), not this.

**Known gaps.**

- **The migration has not been run by the assistant that wrote it.** Creating a scratch database
  needs the postgres superuser, which the permission classifier blocks in an unattended session, and
  `.devtools`' `ledgr` database predates migrations 0051 and 0052. The unit suite (2516 passed)
  covers the service, and `tests/integration/test_sales_invoice_payment.py` (13 tests: overpayment
  guard, what may be paid and into what, immutability, single void, no delete, balances, RLS) is
  written but **unrun**. Apply 0051 and 0052, then run it, before trusting the SQL.
- **No `paid` flag on the invoice view.** A client asks `GET .../payments` for the balance; putting it
  on `GET .../sales-invoices/{id}` is a small follow-up if the screen wants one round trip.
- **No overpayment, no split allocation, no one-payment-many-invoices** (FR-BNK-005).
- **No credit-note refund.** A credit note reduces what is owed; refunding money already paid is a
  payment *out*, which this does not model.
- **`gross` reads `sales_invoice_vat_total`**, written at issue. An invoice issued before that table
  was populated would show a zero gross; none exists, as 0037 writes it at issue.
