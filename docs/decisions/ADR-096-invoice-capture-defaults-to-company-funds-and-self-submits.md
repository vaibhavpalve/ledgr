# ADR-096: Invoice capture defaults payment to the business, and submits itself when VAT is certain

- **Status**: Accepted
- **Date**: 2026-09-30
- **Serves**: FR-EXP-001b, FR-EXP-001c (never blocks), FR-EXP-001e (payment method), FR-AP-002
  (automatic reading)
- **Builds on**: [ADR-081](ADR-081-automatic-invoice-reading.md) (the reader),
  [ADR-095](ADR-095-invoice-reading-failures-retries-and-reading-again.md) (checked readings,
  read-again)

## Context

Two pieces of friction in the purchase-invoice capture screen, once automatic reading was
actually working end to end:

1. **"Paid with" asked a question this flow never needed answered.** The screen is for company
   purchases - a ZZP's business and personal accounts both fund the same business, and a BV's
   purchases are paid from company funds by policy. A person paying privately is filing a
   reimbursement claim, which is a different process this screen does not build. Asking the
   question anyway meant a person picking an answer that was always going to be the same one.
2. **A cleanly read invoice still waited on a click.** FR-EXP-001c says the product never blocks
   on extraction being available, but the reverse was also true: extraction being available and
   correct did not save anyone the trip back to press "Submit".

## Decision

1. **This flow no longer asks for a payment method.** `CaptureRepository.add_item`'s INSERT now
   sets `payment_method = 'business_account'` on every new expense, in the same statement that
   already creates the row. `PaymentMethod.BUSINESS_CARD` and `.PERSONAL_REIMBURSABLE` are
   untouched as values - `ExpenseFormService.update` can still set either - but nothing in this
   screen offers them. A personally-paid receipt is a reimbursement claim; this ADR does not build
   that process, only leaves the enum able to carry it later.
2. **A certain VAT rate submits the claim by itself.** After a reading is applied
   (`InvoiceExtractionService._apply`), `_submit_if_vat_is_certain` calls the same
   `ExpenseFormService.mark_ready` a person's own "Submit" button calls, when two things are both
   true:
   - the invoice's rate was one `_TREATMENT_BY_RATE` already maps unambiguously - 21% or 9%. A
     missing rate, or an invoice printing 0%, is deliberately excluded: 0% could be `btw_0`,
     `btw_vrijgesteld`, `btw_verlegd` or an export, and the reading cannot say which - exactly the
     case FR-EXP-001c already left for a person, unchanged by this ADR.
   - `ExpenseView.can_be_marked_ready` - the same completeness check the button is gated on, run
     through the same code path, not a relaxed copy of it.

   Nothing about *what* gets checked before submission changes; what changes is *who* clicks
   the button when the two conditions above hold.
3. **The web form no longer shows a review screen for a claim it did not need reviewing.** Once
   `expense.status` is not `draft`, `ExpenseForm` shows a "read and submitted automatically, there
   is nothing to review" notice instead of the editable fields - saving a draft that has moved to
   `READY` was already refused server-side (`ExpenseAlreadyReady`); this only stops asking.

## Consequences

- **Separation of duties (ADR-014) is unchanged.** `mark_ready` still only grants "submit"; posting
  to the ledger is still the separate `post_expense` route requiring "post" on `journal_entry",
  which this ADR does not touch and does not grant automatically. A misread amount reaching READY
  by itself still needs a second, separately-permissioned action before it becomes a ledger entry.
- **The audit trail says which claims went through unclicked.** `expense.extraction.submitted` and
  the `extract_invoice` audit event both carry it, so a claim that reached READY without a person
  is distinguishable from one that did not, for whoever reviews later.
- **A wrong 21%/9% reading can now reach READY without a person seeing it first.** This is the real
  cost of the decision: FR-EXP-002's approval workflow is not built yet, so today the safety net
  between a certain-looking-but-wrong reading and a posted entry is whoever performs the separate
  `post` action actually looking at the claim, not a dedicated review step. If FR-EXP-002 is built,
  it is the natural place this ADR's automatic submissions should still land.
- **PERSONAL_REIMBURSABLE and BUSINESS_CARD are inert from this screen**, not removed. A future
  reimbursement-claim process can set them through the same `ExpenseFormService.update` this screen
  already uses.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep asking payment method, default the choice to "business account" | Still a click for the common case this ADR exists to remove, for a question this flow's purchases never actually vary on. |
| Auto-submit every reading, regardless of VAT certainty | Removes the one signal - an unambiguous rate - that ties automatic submission to something the reading was actually sure of. An invoice with no readable VAT would submit as confidently as one that was fully read. |
| Post straight to the ledger instead of only marking ready | Would collapse "submit" into "post", which ADR-014's separation of duties keeps apart on purpose. This ADR only removes the click in front of the same submit action a person already had. |
