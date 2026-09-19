# ADR-076: Bad-debt write-off with the VAT reclaim entry

- **Status**: Accepted
- **Date**: 2026-09-20
- **Implements**: SI-10 / FR-AR-013 (S) - write off an uncollectable invoice with the correct VAT
  reclaim entry
- **Builds on**: [ADR-070](ADR-070-sales-invoice-payments.md) (outstanding balance, payments through
  the ledger, void by reversal), [ADR-072](ADR-072-aged-receivables-and-statements.md)
- **Constrained by**: CLAUDE.md rules 1-4, NFR-031 (no floats), NFR-032, FR-GL-003 (reversal, never
  edit), FR-GL-006, IAM-005

## Context

Until now an invoice could only leave the receivable by being paid or credited. A customer who never
pays left it open forever: chased by dunning, in every ageing report, an asset on the balance sheet.
Writing it off has two parts that do not happen at the same time: recognising the loss, and claiming
the VAT back.

## Decision

1. **A write-off is the whole outstanding balance** of one issued, non-credit-note invoice, never part
   of it. It posts `Dr expense / Cr Debiteuren` (party named, FR-GL-006) into the memorial journal
   through `LedgerService`. The expense account is chosen per write-off, like the bank account of a
   payment, and must be an expense account of the administration (checked in the service and by
   trigger).
2. **`invoicing.invoice_balances` is the one definition of outstanding, and now subtracts an unvoided
   write-off.** Dunning, the overpayment guard and the ageing report therefore stop seeing the invoice
   with no change of their own. `receivable_items` counts a write-off from its own date (a report for
   a past date does not change) and `customer_movements` shows it as a credit, so the statement still
   closes at the debtor's sub-ledger balance. The function gains a `written_off` column appended last
   and is recreated in the migration's transaction (NFR-044).
3. **The VAT reclaim is a second entry** and may come later: `Dr Te betalen omzetbelasting` per VAT
   treatment (each line tagged with its `vat_treatment`, so the return can place it, FR-VAT-001) and
   `Cr expense`. It can be requested in the same call (`reclaim_vat`), which is then refused entirely
   - nothing written - while the waiting period runs, or made later on its own endpoint.
4. **The VAT part is proportional to what is unpaid.** A payment is not attributed to a VAT group, so
   the unpaid balance is spread over the groups by gross and each share's VAT by that group's VAT.
   Largest-remainder allocation in whole cents makes the shares sum to the balance exactly and never
   negative (a property test found the naive "round each, residual on the last" going negative).
   Stored in `vat_split` at write-off, so the later reclaim posts what was decided.
5. **Reversible, once.** Voiding a write-off reverses its entry - and the reclaim's, if any, first -
   via `LedgerService.reverse` in the current period (for a reclaim that is the VAT being paid back,
   which is what happens when the customer pays after all). The row is immutable except for recording
   the reclaim once and voiding once; there is no DELETE grant. A voided write-off leaves the invoice
   owed again and free to be written off again (partial unique index on unvoided rows).
6. **Separation of duties.** `post journal_entry` to write off or reclaim, `reverse journal_entry` to
   void - as for payments. The person who raises invoices cannot make a debt disappear.
   A reason is mandatory; the audit log records amounts, not the free text.

## Legal data flagged for review

`VAT_RECLAIM_WAIT_MONTHS = 12` (from the due date) and "an insolvent customer waives it" are the
author's understanding of Wet OB art. 29, **not verified against the current Besluit**. Have a tax
adviser confirm both before this is relied on, exactly as ADR-071 asks for the dunning constants. It
is one module constant (`api.invoicing.bad_debt`).

Also unresolved: which box of the return carries the correction (a reduction of the original box vs
rubriek 5b). The entry tags the VAT line with the invoice's treatment, which is the input the return
builder needs; the builder (FR-VAT-001) must decide.

## Consequences / out of scope

- No partial write-off, and no bulk "write off everything over N days".
- No automatic trigger: nothing writes off on a schedule.
- Requires a memorial journal and an output-VAT account mapped (0040) for a reclaim; missing ones are
  named in the refusal rather than guessed.
- Migration 0058 is additive apart from recreating `invoice_balances` with one more column.