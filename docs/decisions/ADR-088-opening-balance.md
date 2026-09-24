# ADR-088: The opening balance is one entry in an opening journal, posted through the ledger

- **Status**: Accepted
- **Date**: 2026-09-24
- **Implements**: FR-ONB (bringing an existing business in), FR-GL-001/002 (an opening journal is one
  of FR-GL-002's journal types)
- **Builds on**: [ADR-086](ADR-086-posting-defaults-at-onboarding.md), ADR-082 (manual postings and
  reversal)

## Context

Every business that signs up has a history: money in the bank, equipment, loans, share capital.
The product had no way to bring any of it in. The golden path showed the result. A bakery that
had money in the bank the whole time showed **€ −121,00** of cash after booking one receipt from
the bank, and its balance sheet started at zero. Nothing the product said about the business's
position was true until the owner hand-posted a memorial entry, which no owner knows to do.

## Decision

`GET/POST /v1/administrations/{id}/opening-balance` and a screen at `/ledger/opening-balance`.

- **One entry per fiscal year**, posted with `LedgerService.post` (non-negotiable #1). It lands in
  the administration's `opening` journal (created with code `BB` on first use), is dated on the
  fiscal year's first day, and goes in the first period, which must be open.
- **Balance-sheet accounts only.** No revenue or expense line: last year's trading must not become
  this year's profit.
- **No control accounts.** Debtors and creditors carry one balance per customer or supplier
  (FR-GL-006), and a single figure cannot say who owes what.
- **It must balance.** Otherwise the difference goes to an **equity** account the person chooses
  (default 0530 Onverdeeld resultaat). A difference parked on an asset would invent money.
- **Posted means posted.** A second opening for the same year is refused. Redoing it means
  reversing it in the journal (the ledger is append-only) and entering it again. The existence
  check ignores reversed entries.
- **Import from the previous package** happens in the browser. It reads a CSV trial balance
  (Moneybird, Exact, e-Boekhouden or a spreadsheet) by header meaning (code, and either
  debit/credit or a signed balance), in Dutch or English number formats. Rows whose code is not
  a balance-sheet account in this chart are reported back, never posted elsewhere.

Permissions: posting needs `post journal_entry` and a verified e-mail address, like every other
posting path. Reading needs `view chart_of_accounts`, the permission the journal picker uses.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Tell users to post a memorial entry | Nobody who needs this knows what one is. The product showed wrong figures in the meantime. |
| Accept debtor and creditor totals onto the control accounts | They need a party per line (FR-GL-006). The honest route is importing open invoices, which is the next step, not a lump sum. |
| Parse the import on the server | Would need a file upload, storage and a second format parser for a one-time act. The browser reads it, the person checks the filled-in form, and the server validates what is posted. |
| Allow editing a posted opening balance | Postings are immutable (FR-GL-003). Reversal is the correction. |

## Consequences

**Easier.** A new business can start from where it actually is, and the dashboard, balance sheet
and cash figures are right from day one.

**Known gaps.** Open receivables and payables at the start date must be entered as invoices. There
is no bulk import of open invoices yet, and that is the next import step. The opening balance of a
second year is not generated from the closing of the first. Year-end closing does not exist yet.
