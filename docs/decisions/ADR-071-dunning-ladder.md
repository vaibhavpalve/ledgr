# ADR-071: The reminder ladder (dunning)

- **Status**: Accepted, with **legal constants awaiting review** (see "Needs legal review")
- **Date**: 2026-09-19
- **Implements**: SI-04 / FR-AR-010 (M) - configurable reminder ladder with escalation, statutory
  interest and collection-cost calculation, and pause-per-customer
- **Builds on**: [ADR-070](ADR-070-sales-invoice-payments.md) - "overdue" reads
  `invoicing.invoice_balances`; [ADR-040](ADR-040-invoice-delivery.md) - a reminder is a dispatch
- **Constrained by**: CLAUDE.md rules 1 and 3, NFR-031 (no floats), FR-LOC-003 (recipient language),
  FR-UX-007 (no internal strings to users), IAM-005 (isolation test per endpoint)
- **Unblocks**: SI-11 ("chase all overdue" - `invoicing.overdue_invoices` and `send_next` are exactly
  what a bulk action loops over)

## Context

An invoice past its due date that nobody chases is money that is late for no reason. FR-AR-010 asks
for the chasing to be systematic: reminders that escalate, interest and costs the law allows, and a
way to stop when there is a dispute. The hard part is not sending mail, it is deciding *what may be
claimed and when*, because a reminder that demands money nobody is owed is worse than none.

## Decision

### 1. The rules are pure; the service only wires

`api.invoicing.dunning` decides everything and touches no database and no email: which step is due
(`next_step`), what statutory interest has accrued (`statutory_interest`), what collection cost the
law allows (`collection_cost`), and why nothing should be sent (`Blocker`). `DunningService` fetches
facts, asks, and - only when the rules say a reminder may go - sends and records it. 66 rule tests
run with no fixtures; expected figures are worked out by hand in the test comments, not recomputed.

### 2. Escalation is a sequence, never a jump

The next step is always the **lowest step not yet sent**. An invoice 40 days overdue that nobody has
chased receives step 1, not the formal notice: a customer's first contact about a debt is never a
demand with costs. A step is due once the invoice is `days_after_due` days overdue; the due date
itself is not overdue.

### 3. Costs are a consequence of a notice, not a surcharge

Collection cost may be claimed only after a formal notice (an *aanmaning*, the "14-dagenbrief").
`charge_collection_cost` is allowed on exactly one kind of step, `formal_notice`, at most one per
ladder, and that step must be last - refused when the ladder is configured
(`validate_ladder`), and again by CHECK constraints so no writer can sidestep it. The notice states
a **dated** deadline (`pay_by`), and the cost is worded as *conditional* ("if payment is not received
by ..."): it announces what will be charged, it does not add it to what is owed today.

### 4. Statutory interest: no rate, no interest - never a guess

The commercial rate changes every half year and is set by the government. **The rate table ships
empty on purpose.** A rate written into a migration would be wrong within months and would appear on
customers' letters. So:

- `statutory_interest_rate` is global, effective-dated, append-only (update/delete raise), and
  writable only by `ledgr_ops`.
- A step that charges interest and has no rate loaded for the date is **refused**
  (`Blocker.INTEREST_RATE_MISSING`, with a message naming both ways out: load the rates, or turn
  interest off for that step). It is not sent without the interest and not sent with an invented one.
- An administration whose ladder charges no interest never reads the rate table at all.
- The period is split at every rate change and each slice priced at its own rate; the total is
  rounded **once**, so it cannot depend on how many slices there were (a property test asserts it).
  Interest starts the day after the due date. All arithmetic is `Decimal`.

### 5. Business or consumer: inferred, and shown

The rate kind differs (handelsrente vs wettelijke rente), and `customer` has no consumer flag
(0039). `is_business` is **inferred** - a VAT number on the invoice or a KvK number on the customer
master - and the assessment returns `is_business` and `interest_kind` so a wrong classification is
visible on screen rather than buried in a rate. A consumer invoiced with a VAT number typed in would
be treated as a business; the fix is a real flag on the customer, which is a follow-up.

### 6. A reminder is a dispatch, not a second email system

It goes through `InvoiceDeliveryService.dispatch` with a generic `ReminderNotice` on the
`DeliveryRequest`, the same way SI-01's `custom_message` travels. So it inherits: the stored PDF
attached (never re-rendered), the customer's address, the recipient's language (FR-LOC-003), a
dispatch row with a status, an audit entry, and the channel seam - there is still no
`if channel is EMAIL` in the service. A friendly reminder contains no mention of interest or costs at
all (asserted in both languages, and that no standalone zero amount appears).

### 7. Only an accepted dispatch is a reminder sent

`dispatch` returns a record whatever happened; a provider outage leaves it `queued`. Nothing drains
that queue (0041's worker is not built), so a queued reminder never goes out, and recording it as
sent would stop the step ever being retried. A `dunning_reminder` row is written **only** for a
dispatch the provider accepted; otherwise the request answers 502, records nothing, and the step
stays due. `unique (invoice_id, step_position)` is what stops two people, or one person twice,
sending the same step; a race that gets past the service check is caught there and answered as
"already sent".

### 8. Pause per customer, ladder per administration

`dunning_pause` (a row *is* the pause; deleting it resumes where the ladder stood - steps already
sent stay sent). The ladder is one row per rung, replaced wholesale, with a
`dunning_ladder_configured` marker so **"never configured" (use the built-in default) is
distinguishable from "configured to chase nobody"** (an empty ladder is valid). The default is 7 days
friendly, 21 reminder, 35 formal notice with interest and costs - a starting point, every number the
administration's to change.

### 9. Permissions

Reading needs `create sales_invoice`, as viewing an invoice does. Sending a reminder, pausing and
changing the ladder need `send sales_invoice` (ADR-012, no invented permissions): each is a
communication to, or a decision about how hard to press, somebody outside the business.

### 10. Endpoints (each with an isolation test, IAM-005)

```
GET  .../dunning                                          overview: ladder + every overdue invoice assessed
PUT  .../dunning/ladder                                   replace the ladder (validated)
GET  .../sales-invoices/{id}/dunning                      one invoice's assessment
POST .../sales-invoices/{id}/dunning/send                 send the next step, if due
PUT  .../customers/{id}/dunning-pause                     pause (idempotent)
DELETE .../customers/{id}/dunning-pause                   resume
```

Looking sends nothing. The audit log records that a pause reason *was given*, not what it said - a
free-text customer dispute is not something to accumulate under the audit log's own retention
(IAM-093).

## Needs legal review

Three things in `api.invoicing.dunning` encode Dutch law as the author understands it, are named and
sourced in the code, and should be read by a tax or legal adviser before a formal notice is sent to a
real customer - the same posture `api.invoicing.wording` takes:

1. **`WIK_TIERS`, `WIK_MINIMUM`, `WIK_MAXIMUM`** - 15% of the first EUR 2,500, 10% of the next 2,500,
   5% of the next 5,000, 1% of the next 190,000, 0.5% beyond 200,000; minimum EUR 40, maximum
   EUR 6,775. Transcribed from memory of the Besluit vergoeding voor buitengerechtelijke
   incassokosten, **not** from the Staatsblad.
2. **`FORMAL_NOTICE_DEADLINE_DAYS = 15`** - the minimum is 14 full days from the day after the notice
   is *received*, so 15 from sending is the conservative reading; too long costs nothing, too short
   can void the cost claim.
3. **The email wording** of the formal notice and the interest and cost sentences (catalogue keys
   `invoice.reminder.*`, each annotated).

The interest **rates** are not in this list because they are not in the code: they are data.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Seed the interest rates in the migration | Wrong within months, and on customers' letters. |
| Send the formal notice without interest when no rate is loaded | Silently gives up money the business is owed, with nothing to show it happened. |
| Send with the last known rate | A demand for an amount nobody can source. |
| Jump to the formal notice for a long-overdue invoice | A customer's first contact about a debt becoming a demand with costs. |
| A separate reminder email system | A second address book, a second PDF path, and an `if channel` branch the seam exists to avoid. |
| Record a queued reminder as sent | Nothing drains the queue; the step would never be retried. |
| A consumer flag chosen by the user, now | Needs a customer-master change and a UI; inference with a visible verdict ships first. |
| Book interest and costs as receivables at send time | They are claims, not yet owed; booking them is a separate accounting decision (below). |

## Consequences

**Easier.** SI-11's bulk chase is a loop over `overdue` and `send_next`. SI-06's aged receivables
reads the same `overdue_invoices`. A reminder is explainable months later: the row records the
outstanding amount, interest and cost it claimed.

**Harder.** Nobody can be chased for interest until an operator loads rates.

**Known gaps.**

- **Verified against a real Postgres** after migration 0053 was applied to the local database.
  `tests/integration/test_dunning.py` (17 tests: what is overdue, once-per-step, costs only on a
  notice, notice needs a deadline, reminder immutable and undeletable, ladder constraints, rates
  append-only and not writable by the app role, tenant isolation) and
  `tests/integration/test_dunning_repository.py` (13 tests: every `SqlDunningRepository` method as
  `ledgr_app` under RLS, including the idempotent pause, wholesale ladder replacement, and the
  duplicate-step race turning into `ReminderAlreadySent`) pass. The first run found two bugs in the
  *test helpers* (asyncpg wants real `date`/`Decimal` objects, not strings, even inside a `CAST`) and
  none in the migration or the repository. Full suite with DB tests on: 3197 passed, 0 failed.
- **The HTTP layer is not driven end to end.** The six routes are covered by isolation tests
  (refusal only), the service tests (behaviour, with a fake delivery) and the repository tests
  (SQL); nothing sends a real email through `POST .../dunning/send` against the real database.
- **Nothing sends automatically.** A person (or SI-11) presses send; there is no scheduler. Sending is
  deliberately an explicit act until the ladder has been reviewed by a human on real invoices.
- **No loader for interest rates.** `ledgr_ops` inserts rows with SQL; a script like `load_vat_rules`
  is a small follow-up. Until then the "rate missing" refusal is the correct, visible behaviour.
- **Interest and collection cost are not booked.** They are claimed in the reminder, not posted to the
  ledger; recording interest income when it is actually paid is a separate accounting decision and
  belongs with payment allocation (FR-BNK-005).
- **A one-off customer (no `customer_id`) cannot be paused**, because the pause is keyed on the
  customer master. Making them a customer is the fix, as it is everywhere else in FR-AR-006.
- **No consumer flag on `customer`** - see section 5.
- **The queue is not drained**, so a `queued` reminder never goes out (section 7); the API says so
  (502) rather than pretending.
- **A concurrent double-send can email twice** before the database records once; the constraint
  guarantees one *record*, and cannot un-send a message.
