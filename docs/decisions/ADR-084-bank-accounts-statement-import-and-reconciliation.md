# ADR-084: Bank accounts, CSV statement import and reconciliation

- **Status**: Accepted
- **Date**: 2026-09-23

## Context

`/bank` was a literal `ComingSoon` stub with no table, no service, and — unlike Journal (ADR-082)
and Assets (ADR-083) — no route surface at all. Unlike those two, though, the authorization matrix
already anticipated this domain in full: "Connect / revoke bank consent" (`manage bank_consent`),
"View bank transactions" (`view bank_transaction`) and "Reconcile bank" (`reconcile
bank_transaction`) were already graded rows in PRD Appendix A with role grants seeded. A gap
analysis against Exact Online and Yuki named live bank feed + reconciliation as the single
highest-leverage missing capability: both competitors' daily workflow starts with "look at what
the bank says came in and out."

A live bank feed is a real integration (PSD2/AISP — Tink, Enable Banking, Nordigen/GoCardless Bank
Account Data, or a bank-direct API), each with its own onboarding, credentials and consent flow,
and is out of scope to build in this pass. What was in scope: everything downstream of "transactions
exist" — accounts, import, matching, reconciliation, posting.

## Decision

Add migration 0065 (`bank_account`, `bank_statement_import`, `bank_transaction`) and a new module,
`api.bank`, structured like `api.assets`: `model.py`, `repository.py`, `service.py`, `routes.py`,
plus `csv_parser.py` (a pure, dependency-free parser) and `adapters.py` (the seam a future live
feed fills).

**No PSD2/AISP integration is built.** `api.bank.adapters.BankFeedProvider` is a `Protocol` with
one implementation, `UnconfiguredBankFeed`, which always returns no transactions and
`is_configured = False` — the exact shape ADR-081's `InvoiceExtractor` and
`api.customers.peppol.UnconfiguredPeppolDirectory` already established for "the seam exists, the
provider behind it doesn't yet." A future provider would be selected by configuration, the same
way `EXTRACTION_PROVIDER` selects an invoice extractor, and would feed the same `bank_transaction`
import path CSV import already uses — nothing about the table or the reconciliation service would
change.

**Statement import is one canonical CSV shape** (`date,amount,counterparty_name,
counterparty_iban,description`), not an attempt to auto-detect every Dutch bank's own export
dialect. `api.bank.csv_parser.parse_statement_csv` refuses the whole file at the first unparsable
row rather than skipping it — a statement with one silently-dropped line is a missing transaction
nobody knows to look for. Re-importing the same file is a no-op: `external_id` is a SHA-256 hash of
each line's own content (`api.bank.service._external_id`), and `bank_transaction_dedup_idx`
(migration 0065) is what makes a second attempt at the same content fail cleanly rather than
double the account's transactions.

**Reconciliation has two paths, and neither is a new write into the ledger:**

- Matched to a sales invoice: `api.bank.service.BankService.reconcile_with_invoice` calls
  `api.invoicing.payments.SalesPaymentService.record()` — the existing, already-tested payment-
  recording flow SI-04a built, including its balance check, receivable control account, party
  resolution and open-period check. This module adds nothing to that logic except marking the bank
  transaction as the source of the payment.
- Matched to anything else (an expense paid, a bank fee, a transfer): `reconcile_generic` posts a
  plain two-line entry via `LedgerService.post()` directly, against whichever ledger account the
  caller names, into the administration's single active BANK journal — the same "post directly,
  gate on an open period" shape `api.assets.service` established for depreciation and disposal.

**No new authorization capabilities.** Every route in `api.bank.routes` is gated by a permission
that existed in Appendix A before this module did (`manage bank_consent` for creating/managing a
bank account and connecting it — treating "connect a CSV-fed account" as an instance of "connect a
bank" is deliberate; `view bank_transaction` for reads; `reconcile bank_transaction` for import and
both reconciliation paths — importing is treated as part of the reconciliation work area, so a
Bookkeeper's plain `F` on "Reconcile bank" covers it without needing the `C`-conditional "Connect /
revoke bank consent" row). `migrations/0010_role_catalogue.sql` needed no regeneration.

Match suggestions (`GET .../match-candidates`) reuse `invoicing.invoice_balances` (migration
0052) — the one existing definition of "outstanding" every other screen (dunning, aged
receivables, the overpayment guard) already reads — rather than recomputing a balance a second
way.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Auto-detect each bank's raw CSV/MT940 export format | A real integration per bank, the same shape of work a PSD2 adapter would replace wholesale — building it as a stopgap would be throwaway effort once a live feed exists, and would still not cover every bank. |
| A generic multipart file upload through `api.documents` (malware scan, storage, retention) | A bank statement is transient input to an import step, not a document the business retains as evidence the way a receipt or invoice PDF is (CMP-001's seven-year retention targets source documents, not CSVs already turned into ledger postings). Accepting the CSV as a JSON string body is simpler and matches what the parser actually needs. |
| Silently skip an unparsable row during import | The exact failure mode a bank reconciliation screen must never have: a transaction the bookkeeper never sees and has no way to know is missing. Refusing the whole file and naming the row is worse for convenience and strictly better for correctness. |
| A bespoke posting function for reconciliation (a `bank.*` SECURITY DEFINER function, mirroring `ledger.post_entry`) | Reconciliation is an ordinary posting (generic path) or an ordinary payment (invoice-matched path); both already have a public, tested entry point. A new function would duplicate one of the two for no reason. |
| A new "reconcile" role/capability distinct from Appendix A's existing bank rows | The matrix already modeled this domain in full before any code reached it; inventing new capabilities would fragment authorization decisions across two places for the same resource. |

## Consequences

Makes easier: a future PSD2/AISP adapter is a configuration change and one class implementing
`BankFeedProvider`, not a schema or reconciliation-logic change — `bank_transaction` and its
dedup/reconciliation machinery are provider-agnostic by construction.

Known gaps, left for follow-up:
- **MT940 (and other bank-native formats) are not parsed.** Only the canonical CSV shape is
  accepted; a person must massage their bank's export into it first. Documented on the import
  screen itself (`bank.import_hint`), not hidden behind a button that silently fails for most
  banks.
- **No payment INITIATION** (paying a purchase invoice or supplier from within the app) — this ADR
  is reconciliation of money that already moved, not moving it. PRD's non-goal on acting as a
  licensed PISP applies regardless.
- **No multi-currency handling** beyond a currency code carried on the row — a transaction in a
  currency other than the bank account's own is accepted but not converted; `LineInput` posts the
  numeric amount as given, which is only correct when the currencies already match. Consistent
  with `api.ledger.model.SCALE`'s comment that multi-currency (FR-GL-010) is deferred past GA.

**Not yet applied to the local dev database**: migration 0065 could not be applied in this
session, for the same reason ADR-083 recorded for migration 0064 — applying a migration as the
Postgres superuser is blocked by this environment's permission classifier
(`Modify Shared Resources`). `tests/integration/test_bank_routes.py` and
`tests/bank/test_csv_parser.py` are both written; the CSV parser tests are pure-Python and were run
and pass (9/9) without a database. The DB-backed suite has not been run against a live Postgres.
Apply migration 0065 with:

```
psql -h localhost -p 55432 -U postgres -d ledgr -v ON_ERROR_STOP=1 -f apps/api/migrations/0065_bank_accounts.sql
```

then run `TENANT_ISOLATION_TESTS_ENABLED=1 pytest tests/integration/test_bank_routes.py` to
confirm. Two of its tests (`test_match_candidates_suggests_invoice_of_equal_amount`,
`test_reconcile_with_invoice_records_a_payment`) skip themselves if this environment cannot get an
invoice past its own `/sales-invoices/{id}/issue` route (the same "stops at the document store's
encryption key" caveat `tests/integration/test_invoice_approval.py`'s own `_passes_the_gate`
documents) — that is a pre-existing environment gap, not something this module introduced.
