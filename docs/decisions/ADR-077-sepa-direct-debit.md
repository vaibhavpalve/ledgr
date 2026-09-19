# ADR-077: SEPA direct debit - mandates and pain.008 files

- **Status**: Accepted
- **Date**: 2026-09-21
- **Implements**: SI-09 / FR-AR-011 (S) - SEPA direct debit mandate management including mandate
  reference, signature date, first/recurring flag and pain.008 file generation
- **Builds on**: [ADR-070](ADR-070-sales-invoice-payments.md) (a confirmed collection is an ordinary
  payment), [ADR-076](ADR-076-bad-debt-write-off.md) (`invoice_balances` is the one definition of owed)
- **Constrained by**: CLAUDE.md rules 1-4, NFR-031, NFR-032, FR-GL-003, IAM-005

## Context

Direct debit is the one way a Dutch business collects without waiting for the customer to act. It has
two halves that must not be confused: the paperwork (a mandate the customer signed) and the instruction
(a file the business gives its bank). LEDGR does not talk to a bank; it keeps the first and generates the
second.

## Decision

1. **A mandate is evidence of consent and is immutable.** Reference (unique per administration, 1-35 SEPA
   characters, never altered), scheme (core / b2b), kind (recurring / one-off), signature date, debtor name,
   IBAN (checksum-validated) and optional BIC. Only two things change: it is revoked (once), and
   `last_collected_on` advances. A customer who changes bank signs a new mandate, so there is no amendment
   flow. No DELETE grant.
2. **First / recurring is derived, never typed.** FRST until the mandate has a submitted or collected
   item, RCUR after; OOFF for a one-off mandate (usable once). Within one file the first invoice on a new
   mandate is FRST and the rest RCUR. A failed or cancelled first is still a first.
3. **A mandate lapses after 36 months unused** (from the last collection, or signing). Collecting on a
   lapsed mandate is collecting without consent, so it is refused and reported.
4. **A file is a batch, kept exactly as generated.** `build_pain008` produces pain.008.001.02, one PmtInf
   block per (scheme, sequence type), text transliterated to the SEPA character set and length-limited.
   The XML and its SHA-256 are stored and served byte for byte, so a disputed collection can be reproduced.
   The batch row and its items are immutable apart from cancelling / recording an outcome; no DELETE.
5. **One live collection per invoice** (`sepa_collection_live_invoice_idx`), and the item's amount must equal
   the invoice's outstanding balance at insert (trigger, under a row lock shared with payments and
   write-offs). A batch is written in a savepoint: file, items and reservations all succeed or none does. A
   lost race answers 409, not 500.
6. **Leaving an invoice out is reported.** Asked for by name, every refusal is reported with a code
   (`no_mandate`, `mandate_lapsed`, `not_yet_due`, `already_in_collection`, `nothing_outstanding`,
   `not_found`). Selecting everything reports only what needs action (a lapsed mandate). If nothing
   qualifies, 409 carries the reasons and nothing is written. An invoice is not collected before its due
   date.
7. **Outcomes are recorded by a person, and success records a real payment.** Confirming a collection calls
   `SalesPaymentService.record` (method `other`, reference = the end-to-end id, dated the collection date)
   and settles the item in the same savepoint: no payment without its item or the reverse. If the invoice
   was paid another way meanwhile the payment is refused and the item stays undecided. A failure needs a
   reason (the return code) and leaves the invoice owed and free to collect again. A batch with any recorded
   outcome cannot be cancelled; an untouched one can, freeing its invoices.
8. **Separation of duties.** Mandates: `create sales_invoice`. Generating, downloading, confirming and
   cancelling: `post journal_entry`, as for payments - the file instructs a bank to debit customers and a
   confirmation posts a receipt, so the person who raises invoices cannot also collect. The audit log
   records references and amounts, never the debtor's name or IBAN.
9. **The administration needs an IBAN and a creditor identifier.** `sepa_creditor_id` is validated at write
   time (format and ISO 7064 check digits over the national part - the business code is not part of the
   check) in `api.iban`, beside IBAN validation, because onboarding and invoicing must not import each other.

## Legal / scheme data flagged for review

The 36-month lapse, the minimum lead time of one TARGET business day for both schemes, the 60-day upper
bound and the scheme names are the author's understanding of the EPC rulebooks and are **not verified**
against the current versions; a payments adviser or the business's bank should confirm them. TARGET
holidays are not modelled (a weekday counts as a business day). Whether the customer must be pre-notified
before collection (normally 14 days, satisfied by the invoice) is not enforced. The file has not been
validated against the XSD or a bank's validator in this environment: its element order and content are
tested, but a real upload is the true test - try one collection first.

## Consequences / out of scope

- No bank connection and no automatic reconciliation of returns: recording outcomes is manual until bank
  reconciliation (FR-BNK-*) exists; the stored end-to-end id is what a later import will match on.
- No mandate amendment, no FNAL sequence, no R-transaction (refund/reversal) handling beyond "failed".
- No e-mandate / signature capture: the signed date and reference are recorded, the document is not.
- Migration 0059 is additive: one nullable column and three new tables.