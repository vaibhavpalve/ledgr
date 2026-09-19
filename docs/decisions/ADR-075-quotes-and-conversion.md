# ADR-075: Quotes, order confirmations and conversion to invoice

- **Status**: Accepted
- **Date**: 2026-09-19
- **Implements**: SI-08 / FR-AR-007 - quote / order confirmation converted to an invoice
- **Builds on**: [ADR-037](ADR-037-sales-invoices.md) (drafts and lines),
  [ADR-074](ADR-074-recurring-invoices.md) (creating invoices only through `InvoicingService`)
- **Constrained by**: CLAUDE.md rules 1-4, NFR-031 (no floats), NFR-032 (retries must not
  double-act), FR-AR-003/004, IAM-005

## Context

No quote object existed, so this feature created one. The risk is the same as in ADR-074: a second
path that produces invoices would bypass the statutory gate, numbering and posting.

## Decision

1. **A quote is its own record**, with a lifecycle `draft -> sent -> accepted -> converted`
   (`declined` and `cancelled` are terminal). Enforced twice: pure rules in `quotes.py` and a
   database trigger. Lines are editable only while the quote is a draft; once out, it is what the
   customer was offered.
2. **Order confirmations** are the same record with `kind = order_confirmation` and their own
   number series (OF-nnnn / OB-nnnn), assigned by trigger.
3. **Conversion goes through `InvoicingService.create_draft`** in a savepoint and produces a DRAFT,
   never an issued invoice: a numbered, posted invoice nobody looked at is the bulk-click problem.
   Lines are copied exactly as quoted - never re-priced.
4. **Idempotent (NFR-032)**: `converted_invoice_id` is unique and `mark_converted` is a conditional
   `UPDATE ... WHERE status = 'accepted'`. Converting twice returns the same invoice
   (`already_converted`); a failed conversion rolls the savepoint back and leaves the quote accepted.
5. **Acceptance is recorded** (who, their reference, when). The audit log records that details were
   given, not their contents.
6. **Expiry is derived**, not stored; the last valid day is still valid. An expired offer cannot be
   accepted until its validity is extended.
7. **Totals are net only.** The VAT rate is that of the invoice date (CMP-014); a VAT figure on a
   quote would promise a rate that can change.

## Out of scope / consequences

- PDF and email of quotes are not built; the quote is data and an API.
- No partial conversion, and no quote -> order-confirmation chain.
- The invoice does not carry a link back to its quote (the quote points at the invoice).
- The customer master is required, with the same completeness the invoice gate demands.
- Permission is `create sales_invoice`: whoever may raise an invoice may raise the quote for it.
- Migration 0057 is additive (new tables only).