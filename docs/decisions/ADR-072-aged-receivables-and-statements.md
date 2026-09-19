# ADR-072: Aged receivables and the customer statement

- **Status**: Accepted
- **Date**: 2026-09-19
- **Implements**: SI-06 / FR-AR-012 (M) - aged receivables reporting and a per-customer statement of
  account; the AR half of FR-RPT-001, with FR-RPT-002's drill-down to the invoice
- **Builds on**: [ADR-070](ADR-070-sales-invoice-payments.md) (payments and the balance),
  [ADR-039](ADR-039-invoice-posting.md) (the debtor's ledger party)
- **Constrained by**: CLAUDE.md rules 1 and 3, NFR-031 (no floats), ADR-012 (no invented permissions),
  IAM-005 (isolation test per endpoint)

## Context

"Who owes what, and how late?" is the first question a business asks of its receivables, and a
statement of account is what it sends the customer who disputes the answer. Both read the same facts
ADR-070 now records: invoices, credit notes, payments and voided payments. Nothing new needs storing,
so this is two read-only SQL functions, a pure module that does the arithmetic, and two endpoints.

## Decision

### 1. Ageing is by days past the DUE date, in fixed buckets

Current (not yet due), 1-30, 31-60, 61-90, over 90 days late - the Dutch convention. Not by invoice
date: an invoice on 60-day terms is not late at day 45. The due date itself is still *current* (the
customer has until the end of it), consistent with `dunning.days_overdue`, so the ageing report and the
reminder ladder cannot disagree about what "late" means.

An invoice with **no due date** cannot be late, and inventing one (invoice date? 30 days?) would put
it in a bucket on a term nobody agreed. It has its own `no_due_date` bucket - counted in the totals and
shown - rather than being hidden or guessed.

### 2. "As of a date" is a real thing, and it is not "outstanding now"

`invoicing.receivable_items(administration, as_of)` filters **every movement by its own date**:

| Movement | Counted if |
|---|---|
| invoice | `invoice_date <= as_of` |
| credit note | *its* `invoice_date <= as_of` |
| payment | `paid_on <= as_of` **and** it was not voided on or before `as_of` |

So a report for the end of a quarter does not rewrite itself when run next month, and a payment
voided *later* was still a payment on the earlier date. `invoicing.invoice_balances` (ADR-070) is
deliberately different: it counts every unvoided payment regardless of date, because it feeds the
overpayment guard and should err toward counting money as received. The two agree whenever the report
date is on or after every payment's date - an integration test asserts it - and differ only for a
payment dated after the report date, which is exactly where they should.

### 3. A statement is one list of movements

`invoicing.customer_movements` returns the four movements on a customer's account, oldest first:
invoice (debit), credit note (credit), payment (credit, on the day the money arrived) and voided
payment (debit, on the day it was voided). The opening, running and closing balances are all
arithmetic over that one list in `api.invoicing.receivables.build_statement` - there is no separately
queried "balance brought forward" that could disagree with the lines beneath it. Everything before the
period folds into the opening balance, so any period's statement starts from what was truly owed.
Amounts are unsigned in both columns, as the ledger's are; a positive balance means the customer owes
and a negative one that they are in credit (a credit note after a payment), shown rather than hidden.

### 4. The totals are checked, not hoped for

Bucket totals, per-customer totals and the grand total are the same money cut three ways.
`AgeingReport.__post_init__` refuses to construct one where they disagree, and `Statement` refuses one
where opening + debits - credits is not the closing balance - the device `InvoiceTotals` uses. Property
tests drive both with arbitrary data.

### 5. Reconciles to the ledger, and a test says so

A customer's closing balance equals `ledger.subledger_balance` for their party, because the four
movements are exactly what the ledger records against that party (ADR-039, ADR-070).
`test_a_statements_closing_balance_reconciles_to_the_ledger_subledger` compares them against Postgres.
If they ever disagree, one of them is wrong.

### 6. Grouping: the master record is the identity

Customers are grouped by `customer_id` when there is one, else by the (trimmed, case-folded) name on the
invoice. A one-off customer invoiced twice under one name is one debtor on the report - kinder than the
ledger, which makes them two parties (ADR-039) - while two *customer records* sharing a name stay two.
Customers come out largest debt first, ties by name; invoices within a customer oldest-due first.

### 7. Permission: `view report`

Appendix A's "View reports" (Owner, Accountant, Bookkeeper full; Approver conditional; Viewer read).
Not `create sales_invoice`, which would let an Invoicer read every customer's debt and payment history
for having drafted an invoice - and no permission is invented (ADR-012). A denied attempt is audited;
a successful read is not (it changes nothing, like listing invoices).

### 8. Endpoints (each with an isolation test)

```
GET .../receivables/ageing?as_of=YYYY-MM-DD                  aged receivables, drilling to invoices
GET .../customers/{id}/statement?from=YYYY-MM-DD&to=YYYY-MM-DD   statement of account
```

`as_of` defaults to today; `to` to today and `from` to 1 January of that year. An inverted period is a
422 before any data is read; an unknown customer is a 404 indistinguishable from another tenant's.
Bucket and line-kind labels are translated server-side in the reader's language.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Age by invoice date | Wrong for anything but net-zero terms; a 60-day invoice would look 45 days late. |
| Reuse `invoice_balances` for the report | Cannot answer a past date, so a period-end report would change when re-run. |
| Compute the opening balance with a separate query | A second definition that can disagree with the lines under it. |
| Store a running balance per customer | A balance to keep in step with the ledger; derived is always right. |
| Put a no-due-date invoice in "current" | Hides it behind a term nobody agreed. |
| Read the ledger's sub-ledger directly for the report | It is per party, not per invoice, so it cannot age items; it is the reconciliation target instead. |
| `create sales_invoice` as the permission | Lets the Invoicer read every customer's debt. |

## Consequences

**Easier.** SI-11 can order its chase by the same ageing. A period-end ageing can be reproduced and
attached to a review. A disputed balance is answered with a statement that reconciles to the books.

**Harder.** `customer_movements` reads all of a customer's movements for every statement; fine for a
customer's lifetime of invoices, and the place to add a date bound if one customer ever has thousands.

**Known gaps.**

- **No PDF and no email of the statement, and no export.** JSON only. A statement PDF reuses the
  invoice renderer and belongs with FR-RPT-005's export (which must also log a data-access event); it
  is a follow-up, not part of this.
- **No one-off customer statement.** A statement needs a customer master (`customer_id`); a one-off
  appears in the ageing under their name but has no statement, as they cannot be paused or chased.
- **A void's date is `voided_at::date` (the database's clock).** The ledger reversal is dated by the
  application (`date.today()`); across a timezone boundary the two could differ by a day, which would
  make a statement and the sub-ledger disagree for that one day. Not seen, and cheap to fix by dating
  both from one place.
- **Reconciliation is tested without a void.** The test covers invoice and payment; a real reversing
  entry needs `ledger.reverse`, which the integration seed does not drive.
- **Fixed buckets.** 30/60/90 is the standard; making them configurable is a setting nobody has asked for.
- **Verified against the native local Postgres**: migration 0054 was applied and its 12 DB tests (as-of
  reproducibility incl. a later void, agreement with `invoice_balances`, credit notes by date, the four
  movement kinds, ledger reconciliation, the repository, RLS) pass on the first run.
