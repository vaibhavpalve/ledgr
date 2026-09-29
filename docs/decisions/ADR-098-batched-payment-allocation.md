# ADR-098: One bank line settling several invoices at once

- **Status**: Accepted
- **Date**: 2026-09-30
- **Serves**: FR-BNK-005 (the batched-payment shape - "one transaction covering many invoices")
- **Builds on**: [ADR-091](ADR-091-bank-statement-formats-and-matching.md) (matching with a
  confidence), [ADR-097](ADR-097-partial-payment-matching.md) (partial payments - the previous
  FR-BNK-005 shape)
- **Defers**: overpayment (still nowhere automatic to put the extra), split allocation onto a
  non-invoice target, and the equivalent for receipts on the expense side

## Context

ADR-097 let a bank line pay LESS than one invoice owed. The next gap in the same requirement is
the opposite direction of the same problem: a customer's single transfer covering several
invoices at once - a common real shape (a monthly settlement, a batch of small invoices paid
together), and one `bank_transaction.matched_sales_invoice_id` cannot represent at all, since it
is one column naming one invoice.

## Decision

**A new table, not a new posting path.** `bank_transaction_allocation` (migration 0071) holds one
row per (bank line, invoice) pair - its own amount, its own `SalesPaymentService.record()` payment
and journal entry. Every entry it points at was posted through that same existing call
`reconcile_with_invoice` already uses; this ADR calls it more than once for one bank line, it does
not add a second way to post a payment (CLAUDE.md non-negotiable #1).

**The whole batch is validated before anything is posted.** `BankService.reconcile_with_invoices`
refuses, before calling `SalesPaymentService.record()` even once:
- fewer than two allocations - a single invoice is `reconcile_with_invoice`'s job, not a batch;
- the same invoice named twice - it cannot mean two different things;
- allocations that do not sum to EXACTLY the bank line's amount - a batched payment settles the
  whole line, or a person corrects the amounts and tries again, never a partially-applied batch
  left in an ambiguous state.

Each individual allocation may itself be a partial payment of that invoice (ADR-097's shape,
inside a batch) - `SalesPaymentService.record()` already accepts anything up to the invoice's own
balance, checked independently for each one.

**`bank_transaction`'s own columns stay null for a batch, except `journal_entry_id`.** Neither
`matched_sales_invoice_id` nor `matched_payment_id` can name more than one invoice, so a batched
reconciliation leaves both null; the full breakdown lives in `bank_transaction_allocation`.
`journal_entry_id` still points at the batch's FIRST entry, because `bank_transaction`'s own CHECK
constraint (0065) requires it non-null once `status = 'reconciled'` - a single column was never
going to carry the whole story, and this ADR does not ask it to.

**The web app shows a live running total, never treats it as authoritative.** Checking an invoice
in the split panel defaults its amount to what it is still open for; a person can edit any amount
before confirming. The displayed total is computed with `Number()` for that display alone - the
values actually sent are the typed decimal strings (NFR-031), and the server's own exact `Decimal`
sum is what is actually checked. A client-side rounding quirk can at most mislabel the button's
enabled state for a moment; it cannot cause a wrong amount to post, because the server re-validates
the same sum independently.

## Consequences

- A customer's single transfer covering several invoices, quoting none or all of their numbers,
  can now be recorded correctly against every invoice it paid, in one action.
- **Overpayment is still not built.** Nothing in this ADR gives an allocation batch anywhere to put
  money beyond what the named invoices owe - the sum must still be exact. A credit-balance concept
  is a separate, larger piece of infrastructure this codebase does not have yet.
- **A batch can only settle sales invoices.** The expense/outflow side (ADR-092's Kruisposten
  settlement) has no equivalent: a receipt has no running outstanding balance the way an invoice
  does, so "which receipts did this outgoing transfer pay" has no natural allocation table to
  reuse without first designing that concept for expenses.
- **Reopening a previously-batched transaction's breakdown needs a query against
  `bank_transaction_allocation` directly** - there is no dedicated read endpoint for it yet. The
  reconcile response returns the reconciled transaction, which is enough to confirm the action
  succeeded; showing "this line paid invoices A, B, C" on a later visit is a follow-up, not
  something this ADR's scope required to ship the writing side first.
- Correcting a batched reconciliation, like any posted entry, is a reversing entry (FR-GL-003) -
  `bank_transaction_allocation` is insert-only, the same posture the ledger itself takes.

## Alternatives considered

| Alternative | Why not |
|---|---|
| A generic `bank_transaction_allocation` covering both invoices and receipts, with a `kind` column | The expense side has no partial/batched shape built at all yet (ADR-097's own deferral) - a generic table today would carry a `kind` value nothing produces, and could not be validated against real behavior until the expense side exists. |
| Let the batch total be less than the line's amount, leaving a remainder unmatched | Would need a third transaction status between `unmatched` and `reconciled`, rippling into every place that already filters on those two. Requiring an exact sum keeps `bank_transaction`'s existing two-state model intact. |
| Compute and send the running total from the client as the authoritative amount | Would make the server trust arithmetic done in a browser with `Number()`. The server already re-derives and checks the exact sum in `Decimal`; the client's total is a convenience, never load-bearing. |
