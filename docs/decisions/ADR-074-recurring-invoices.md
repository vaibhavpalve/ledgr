# ADR-074: Recurring and subscription invoicing

- **Status**: Accepted
- **Date**: 2026-09-19
- **Implements**: SI-07 / FR-AR-008 (S) - recurring/subscription invoicing with schedules,
  indexation and end dates
- **Builds on**: [ADR-037](ADR-037-sales-invoices.md) (drafts and lines),
  [ADR-039](ADR-039-invoice-posting.md) (issuing posts), [ADR-071](ADR-071-dunning-ladder.md)
  (the savepoint pattern)
- **Constrained by**: CLAUDE.md rules 1-4, NFR-031 (no floats), NFR-032 (retries must not
  double-act), FR-AR-003/004 (the statutory gate and gapless numbering), IAM-005

## Context

A subscription is the same invoice, raised on a rhythm, at a price that occasionally steps up. The
tempting implementation is a scheduler that writes invoices. The dangerous part is that "an invoice"
carries a statutory gate (FR-AR-003), a gapless number (FR-AR-004) and a ledger posting (FR-GL-006),
and a second code path that produced invoices would have to reimplement all three - or quietly skip
them. So the schedule is a *template*, and generating from it is the same act a person performs.

## Decision

### 1. A schedule is a template; a run is `create_draft` (and optionally `issue`)

`RecurringInvoiceService` holds no invoice logic. A run calls `InvoicingService.create_draft` with the
schedule's customer and lines, and - only when the schedule says so - `InvoicingService.issue`. The
statutory gate, the number allocation, the posting and the permission checks are exactly the ones a
person meets; a schedule cannot bypass any of them, and an end-to-end test confirms the generated
invoices are ordinary drafts with ordinary lines and totals.

**Sending is never automatic.** A run creates a draft or issues an invoice; it never e-mails one. A
customer's first sight of a bill is a decision (`send`), not a side effect of a date arriving. The
service's constructor has no delivery service, and a test asserts it.

### 2. `auto_issue` is off by default, and a refused issue keeps the draft

Issuing is a claim to somebody outside the business, so a subscription that starts billing itself is a
decision, not a default. When it is on, issue is attempted in a **nested savepoint**: if it is refused
(no KvK number, a locked period, an unmapped account, an unrenderable logo) only the issue rolls back -
and the number allocation with it, so the gapless series has no hole - and the invoice stays a draft,
recorded with a code and a sentence in both languages. `left_as_draft` in the run summary is the count
of invoices somebody must finish. The run itself succeeded; the schedule advanced.

### 3. Dates are derived from the anchor, never chained

Occurrence `n` is `start_date + n x interval` months, computed from the start date every time
(`api.invoicing.recurrence`). Chained ("previous date plus a month") drifts: a 31 January schedule
would go 28 Feb, 28 Mar, 28 Apr forever. Anchored, it goes 28 Feb, 31 Mar, 30 Apr. The interval is one
of 1, 2, 3, 4, 6, 12 months - the rhythms a business actually bills on - enforced in code and by CHECK.

### 4. End: the first of two limits

`end_date` (inclusive) and `max_runs` may both be set; whichever is reached first ends the schedule.
An ended schedule has no next date (a CHECK ties `status = 'ended'` to `next_run_on is null`), and one
that is extended by an edit is **not silently revived** - it becomes `paused`, and resume is a
deliberate act.

### 5. Indexation compounds once per completed year, rounded once

`indexation_percent` raises every unit price on each anniversary of the start date: base for the first
year, x (1 + p/100) from the first anniversary, x (1 + p/100)^2 from the second. The **base** price is
stored, so a schedule always shows what was agreed and each invoice what was charged. The result is
rounded once, to the four decimals `sales_invoice_line.unit_price` holds (a price of 0.0350 is
ordinary), never mid-compounding. The schedule's JSON carries both `unit_price` (agreed) and
`next_unit_price`, so a screen can show a coming increase before the customer is billed it.

### 6. Idempotent by a unique key; atomic in a savepoint

`unique (recurring_invoice_id, run_date)`: a schedule generates one invoice per scheduled date, ever.
A retry, a double click and two workers racing reach the same constraint; the loser fails rather than
billing twice. The draft, its run row and the schedule's advance are written in one savepoint, so the
loser's draft is rolled back with it, and the counter is brought forward so that date is not offered
again. `advance` only ever moves forward. Calling the run twice generates each date once.

### 7. Catching up is dated in the past, capped, and ordered

A schedule unrun for three months generates three invoices, each **dated on its own scheduled date**
(the invoice date is the run date), not one dated today. At most 12 runs per schedule and 100 per call;
the rest wait for the next call, oldest first. If a run fails (an archived customer, no fiscal year for
the date) that schedule's *later* dates are not attempted - March's invoice must not precede
February's in a numbered series - while other schedules carry on. The failure is stored on the schedule
as a code (never prose, FR-UX-007) with its sentence, cleared by a success; the same date is retried.
A pause **postpones** billing and does not waive it: resuming generates what fell due, each in its own
month.

### 8. A definition is validated when saved

Interval, end before start, a run count below one, indexation outside 0-100%, a payment term outside
0-365 days, no lines, a blank description, a zero quantity, a discount outside 0-100%, and floats are
refused with `recurring_invalid` when the schedule is saved - not discovered on the first of the month.
A negative unit price is allowed (a standing discount line). The customer must be a customer of *this*
administration (a clean 404, and a database trigger behind it).

### 9. The rhythm locks once the schedule has billed

Dates derive from the start date and interval, so changing either - or the customer - after invoices
exist would re-date the whole history. They are locked (`recurring_locked`, 409); lines, prices, the
end date, indexation, terms and notes may change and affect **future** runs only. To change the rhythm,
end the schedule and start a new one. A schedule is ended, never deleted (no DELETE grant): its runs
point at invoices that exist.

### 10. Permissions, and no scheduler yet

Everything needs `create sales_invoice` (ADR-012). A schedule with `auto_issue` also needs `send
sales_invoice` from whoever runs it; without it the invoice stays a draft (`not_authorized_to_issue`),
not a refusal. The run route also carries `require_verified_email` (IAM-010b), as `issue` does, since
it can post. **Nothing runs automatically:** a person - or a future scheduled job acting for a named
user, because a ledger posting must have an actor (FR-GL-004) - calls `POST .../recurring-invoices/run`.

### 11. Endpoints (each with an isolation test)

```
POST .../recurring-invoices                     create        GET   .../recurring-invoices          list
GET  .../recurring-invoices/{id}                read          PUT   .../recurring-invoices/{id}     replace (future runs)
POST .../recurring-invoices/{id}/pause          pause         POST  .../recurring-invoices/{id}/resume   resume
POST .../recurring-invoices/run                 generate every invoice that should exist by today
```

## Alternatives considered

| Option | Rejected because |
|---|---|
| A scheduler that writes invoices directly | A second path around the statutory gate, numbering and posting. |
| Chain each date from the previous | Drifts off a month-end anchor permanently. |
| Index by simple (non-compounding) percentage | Not how a contractual annual indexation works; would under-charge from year two. |
| Round the price every year | Drifts from compounding then rounding; a price would depend on the path taken. |
| Skip missed runs on resume | Waives billing the business is owed; a pause is not a waiver. |
| Generate all missed runs in one go | Dozens of backdated invoices in one request. |
| Continue past a failed run | Would put a later invoice before an earlier one in a numbered series. |
| Issue by default | A subscription that starts billing itself should be a decision. |
| Send by default | A customer's first sight of a bill is a decision, not a side effect. |
| Delete an ended schedule | Its runs reference invoices that exist. |

## Consequences

**Easier.** A subscription is defined once and a person (or later a scheduled job) generates its
invoices with one call; each is an ordinary draft they can review, edit and send. A price step is
visible before it is billed.

**Harder.** A schedule's invoices are ordinary invoices, so SI-12's duplicate warning will fire on
them (identical lines for the same customer within 30 days) - correct, and noisy for a monthly
subscription; suppressing it for schedule-generated invoices is a follow-up.

**Known gaps.**

- **No scheduler.** Generation is an explicit call; there is no daily automatic run, and no system
  actor for one (FR-GL-004 needs a named actor for a posting).
- **No automatic send.** By design; a "send on generation" option would be a separate, explicit choice.
- **A generated invoice may trigger SI-12's duplicate warning** (see above).
- **Indexation is a single fixed annual percentage.** A CPI-linked index (a published figure applied
  each year) is a different feature: the percentage would be an input each anniversary, not a constant.
- **No proration**, and no mid-period start (billing in arrears or advance is expressed by the start
  date and the lines, not modelled).
- **Verified against the native local Postgres** (migration 0056 applied): 64 rule tests, 38 service
  tests, 19 DB tests (CHECKs, tenant coherence, the idempotency key, immutability, RLS, every
  repository method) and 10 end-to-end tests through real HTTP with the real `InvoicingService` and
  nothing faked. Building the end-to-end test found the repository answering `create` with the input
  (`49.95`) while `GET` returned what the database stores (`49.9500`); both now re-read the row.
