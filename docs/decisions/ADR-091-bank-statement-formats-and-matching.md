# ADR-091: Bank statements as the bank exports them, and matches with a confidence

- **Status**: Accepted
- **Date**: 2026-09-24
- **Implements**: FR-BNK-002 (CAMT.053, MT940, CSV import), FR-BNK-003 (matching with a
  confidence score), FR-BNK-004 in part (conservative defaults; propose, never post unasked)
- **Builds on**: [ADR-084](ADR-084-bank-accounts-statement-import-and-reconciliation.md) (bank accounts, import and the two
  reconcile paths)
- **Defers**: FR-BNK-001 (PSD2 feed), FR-BNK-005 (partial and batched payments), FR-BNK-006
  (learning rules), configurable thresholds and auto-posting from FR-BNK-004

## Context

ADR-084 imported one canonical CSV (`date,amount,counterparty_name,...`), which no Dutch bank
exports. So a customer had to reshape their bank's file in a spreadsheet before LEDGR would read
it, and nobody does that twice. Once the lines were in, every one of them had to be opened and
matched by hand, even when the payment carried the invoice number.

Until a PSD2 feed exists (FR-BNK-001 needs an AISP contract), importing the file is the only way
bank data gets in. That makes it the path that has to work.

## Decision

**Import reads what the bank gives you** (`api.bank.statement_formats.parse_statement`).
- **CAMT.053**, any `camt.053.001.xx` version. Only booked entries (`BOOK`) are read. A file with
  a DTD or entity declaration is refused, which blocks XXE and billion-laughs attacks. So is a
  file with more than one account in it.
- **MT940**, reading `:61:` and `:86:`, with the `/NAME/`, `/IBAN/`, `/REMI/` and `/CNTP/`
  subfields.
- **CSV exports** from ING, Rabobank, bunq and Knab, ABN AMRO's TXT export, and ADR-084's
  canonical CSV. The format is recognised from the header row.
- **Encoding**: the web app decodes the file as UTF-8, falling back to Windows-1252 (ING's CSV),
  so names like "Privé" survive.
- **The account check**: when the file names an account and the bank account has an IBAN, the
  two must be the same. Importing a different account's statement is refused
  (`errors.bank_statement_other_account`); otherwise it would corrupt this account's
  reconciliation.
- **Parse errors** are refusals with a reason, never a 500.
- **Deduplication** is unchanged: the same line imported twice is booked once.

**Each unmatched incoming line gets its best open invoice, with a confidence**
(`api.bank.matching`). This part is pure.
- **Candidates** are open sales invoices whose outstanding amount *equals* the incoming amount.
  Partial and batched payments are left to a person (FR-BNK-005).
- **Evidence**:
  - `reference`: the invoice number appears in the description.
  - `name`: the payer is the customer, ignoring legal-form words and case.
  - `only_candidate`: no other open invoice has this amount.
- **Levels**:
  - **high**: a reference match, or a name match that is also the only candidate.
  - **medium**: a name match, or the only candidate.
  - **low**: only the amount matches.
- **Competing HIGH matches are lowered to medium**, with the reason `ambiguous`. This covers two
  invoices that are both certain for one payment, and one invoice that is certain for two
  payments (a customer paid twice). "Certain" means no person has a choice to make.

`GET .../bank-accounts/{id}/transactions` returns each line's `suggestion`, computed in one read
of the open invoices. `match-candidates` returns every candidate with its confidence and
reasons.

**Nothing is posted without a click.** The web app shows each suggestion with its level and
reasons. It offers one-click matching per line, and a "match all certain" button that applies
only HIGH suggestions. Each one goes through the existing `reconcile-with-invoice` call, so
bulk matching adds no new write path into the ledger. A refusal, such as an invoice paid in the
meantime, leaves that line for a person and the rest carry on.

That is the conservative default FR-BNK-004 asks for. Configurable thresholds and posting without
a click can come once there is match history to calibrate them against.

**The generic reconcile path no longer preselects an offset account.** The previous default was
the first account in the chart, a fixed-asset account, so one careless click booked a bank line
to machinery.

## Consequences

- **Tenancy, authorization and ledger properties are unchanged.** Suggestions are read under the
  administration's RLS context, through the existing `view bank_transaction` permission.
  Matching writes only through `SalesPaymentService.record()`, as before.
- **Purchase invoices** (the AP side of FR-BNK-003) are not matched yet. An outgoing line is
  reconciled through the generic path.
- **IBAN history and learning from corrections** (FR-BNK-006) would raise confidence on repeat
  payers. Neither is built, so a first-time payer who leaves out the invoice number gets at most
  "likely", unless they are the only candidate.
- **Adding a bank format** means adding a parser to `statement_formats` and a fixture test. The
  import route and the UI don't change.
