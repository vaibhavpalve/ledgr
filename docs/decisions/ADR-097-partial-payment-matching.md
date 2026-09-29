# ADR-097: A referenced partial payment matches its invoice, still only a person's click

- **Status**: Accepted
- **Date**: 2026-09-30
- **Serves**: FR-BNK-005 (partial payments - the first of its four named shapes), FR-BNK-003/004
  (matching with a confidence, conservative defaults)
- **Builds on**: [ADR-091](ADR-091-bank-statement-formats-and-matching.md) (matching with a
  confidence), [ADR-092](ADR-092-bank-paid-expenses-settle-through-kruisposten.md) (the expense
  side of matching)
- **Defers**: overpayments, batched payments (one line paying several invoices) and split
  allocation - the other three shapes FR-BNK-005 names - and the equivalent for receipts on the
  expense side, which has no running outstanding balance to pay down yet

## Context

ADR-091 matched a bank line to an open invoice only when the amounts were exactly equal - deferred
on purpose, since a wrong partial guess is worse than no guess. But an exact-amount-only rule
means a customer paying part of what they owe, with the invoice number right there in the payment
description, gets nothing: not a suggestion, not a way to record it against the right invoice at
all. The only route left was "generic reconcile" - a plain posting against a person-chosen account
that never touches the invoice's own balance, silently leaving it looking unpaid in the
receivables sub-ledger while the money already moved.

`api.invoicing.payments.SalesPaymentService.record()` already takes an explicit `amount` distinct
from the invoice's balance, and already accepts anything up to (never over) it - partial-payment
recording was never missing at the invoicing layer. What was missing was the bank-matching layer
ever offering it.

## Decision

**`api.bank.matching.fits`** now admits a sales invoice whose open amount is GREATER than what the
line pays, but only when the line's own reference names it (`reference_in`). A greater amount with
no reference is not a candidate at all - amount stops being a unique signal once exact equality is
no longer required, so without the reference there is nothing principled to rank it on. A LESSER
open amount than what the line pays (an overpayment) still never fits: there is nowhere automatic
to put the extra, and that stays out of scope here.

**A partial match is capped at MEDIUM, by construction, never HIGH.** `score` tags it `"partial"`
and never promotes it, whatever else is true about it - real money stays open on the invoice
afterward, so FR-BNK-004's conservative default means this is always a person's click, never
something `certain()`/"match all certain" can apply in bulk.

**`BankService.match_candidates` now reads every open invoice**, not only an equal-amount one
(`open_invoice_balances` rather than the old exact-amount SQL query, which is now dead and
removed), and filters with `fits` in Python - the same shape `suggest()` (the bulk per-line
suggestion path) already used. The expense/outflow side is untouched: still fetched pre-filtered
to the exact amount, because a receipt has no partial-payment shape to offer yet.

**Reconciling a partial match uses the EXISTING route and service call, unchanged.**
`reconcile_with_invoice` already only passes `transaction.amount` through to
`SalesPaymentService.record()`, which already accepts anything up to the balance - nothing needed
to change there. The web app shows both figures side by side (`bank.partial_amount`: "pays €500.00
of €1,149.50 open") rather than computing the remainder itself, so no money arithmetic runs in the
browser (NFR-031) - the server's own numbers are the only ones shown.

## Consequences

- A customer paying in instalments, quoting the invoice number each time, is now matched
  correctly instalment by instalment - each one a MEDIUM-confidence, person-confirmed match, the
  invoice's outstanding balance falling by exactly what each instalment paid.
- **Overpayment, batched payment (one line settling several invoices) and split allocation are
  still not built.** They are real, separately-scoped pieces of FR-BNK-005 - an overpayment needs
  somewhere to put the extra (a credit balance concept this codebase does not have yet); a batched
  payment needs a transaction to settle more than one invoice, which the current single
  `matched_sales_invoice_id` column cannot represent without its own migration. Both are next on
  the bank feature list, not folded into this ADR to keep this change reviewable and low-risk.
- **The expense/outflow side has no partial shape either.** A receipt paid in instalments from the
  business account is not covered - `ExpenseNotMatchable`'s exact-amount check
  (`reconcile_with_expense`) is unchanged. Receipts have no "outstanding balance" concept the way
  invoices do (`invoicing.invoice_balances`), so building this properly means designing that
  concept first, not reusing this ADR's mechanism by analogy.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Surface a partial match on a name match alone, without requiring the reference | Amount is no longer a unique filter once partial amounts are admitted; a name match alone against every open invoice larger than the line would be noisy and unreliable - the reference is the one signal strong enough to justify a candidate. |
| Let the web app compute and show the remaining balance | Would be client-side arithmetic on money (NFR-031's concern is float precision, and re-deriving a number the server already knows invites exactly the kind of drift this codebase avoids elsewhere). Showing both server-given figures side by side needs no subtraction at all. |
| Allow a partial match up to HIGH confidence when the reference is present | Confidence is meant to say "safe to apply without a second look." A partial match always leaves the invoice open for more, which is a materially different outcome from a full settlement - it should never be indistinguishable from one in the confidence signal. |
