# ADR-092: Bank-paid receipts settle through Kruisposten, matched to their bank line

- **Status**: Accepted
- **Date**: 2026-09-24
- **Implements**: FR-BNK-003 (the payables side of matching), FR-EXP-001e (the payment method
  determines the posting)
- **Builds on**: [ADR-086](ADR-086-posting-defaults-at-onboarding.md) (posting defaults),
  [ADR-091](ADR-091-bank-statement-formats-and-matching.md) (matching with a confidence)

## Context

ADR-086 mapped the payment methods "business account" and "business card" straight onto the bank
account (1100). Posting such a receipt credited the bank on the receipt's date.

When the bank statement came in, the same payment arrived again as an outgoing line. Matching only
knew sales invoices, so the only way to process that line was a generic posting. Picking the
expense account there, which is what a person naturally does, booked the cost twice and took the
money out of the bank twice. Getting it right meant booking 1100 against itself, which nobody
would work out. The golden path missed this because its statement never included the receipt's
payment.

## Decision

**A bank-paid receipt credits Kruisposten (2000, RGS `BLimKrp`, "transfers in transit").**
- `app.ensure_posting_defaults` (migration 0070) maps `business_account` and `business_card` to
  Kruisposten. It falls back to the bank when a chart has no transit account.
- Existing mappings are repointed only where they still hold ADR-086's value, the bank account. A
  mapping someone set on purpose is left alone.
- Kruisposten is part of RGS "Liquide middelen". The dashboard's cash figure sums that group, so
  cash still drops the moment a receipt is booked. The bank account itself now equals the
  statement, which the reconciliation statement in FR-BNK-007 will need.

**An outgoing bank line is matched to the receipt it paid.**
- **Candidates**: receipts that are posted, paid from the business account or card (never out of
  someone's own pocket), not yet matched, with a gross amount exactly equal to the line.
- **Scoring**: the same pure scorer as sales invoices (ADR-091). Direction decides the kind:
  money in only meets sales invoices, money out only meets receipts. The supplier's name and the
  supplier's invoice number are the evidence, and the confidence levels and bulk rule are
  unchanged.
- **The new route**: `POST .../bank-transactions/{id}/reconcile-with-expense` settles the receipt.
  - When the receipt credited Kruisposten, it posts **Dr Kruisposten / Cr Bank** through
    `LedgerService.post` into the bank journal, dated on the bank line. The idempotency key is
    per bank line.
  - When the receipt credited the bank directly (booked before 0070), the payment is already in
    the ledger. The line is linked to the receipt's own entry and **nothing is posted**. Existing
    entries are never rewritten; the ledger is append-only.
- **Uniqueness**: `bank_transaction.matched_expense_id` records the link. A unique index allows
  one bank line per receipt, and a check stops a line from settling both an invoice and a
  receipt.

**The candidate contract is neutral about kind.** Each candidate carries `kind` (`sales_invoice` or
`expense`), `document_id`, `reference`, `party_name`, `amount` and `document_date`, replacing the
invoice-only field names from ADR-091. The web app is the only client, and it dispatches the match
by kind.

## Consequences

- **Tenancy, authorization and ledger.** No new table; the new column sits under
  `bank_transaction`'s existing RLS policies. The new route uses the existing
  `reconcile bank_transaction` permission, with an isolation test. The settlement goes through
  `LedgerService.post`, and nothing writes to posting tables directly.
- **Unmatched items stay visible.** A receipt whose bank line never arrives, or is never matched,
  leaves a credit on Kruisposten. That is the correct signal: money said to have left, which the
  bank has not shown.
- **Not yet covered.** A card's monthly settlement covers many receipts, and the owner may be
  reimbursed in a batch. Both are one bank line against many documents (FR-BNK-005), still
  handled through the generic path against Kruisposten or the reimbursement account.
- **Purchase invoices** (FR-AP, payment due later) will settle the same way, through creditors
  instead of Kruisposten, reusing this matching path.
