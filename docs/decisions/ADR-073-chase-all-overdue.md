# ADR-073: "Chase all overdue" - bulk reminders, and the two defects found building it

- **Status**: Accepted
- **Date**: 2026-09-19
- **Implements**: SI-11 (TRD, "Differentiators beyond the PRD") - one click sends the next
  reminder-ladder step to every overdue customer at once
- **Builds on**: [ADR-071](ADR-071-dunning-ladder.md) (the ladder and `send_next`),
  [ADR-070](ADR-070-sales-invoice-payments.md) (what is owed)
- **Amends**: ADR-071 - two defects in the single-send path, fixed here (sections 1 and 2)
- **Constrained by**: CLAUDE.md rules 1 and 3, NFR-032 (retries must not double-act), IAM-005

## Context

Sending one reminder is a considered act. Sending fifty is a different kind of act: a mistake is
multiplied, an email cannot be recalled, and one of the fifty is always the customer whose address is
wrong or whose dispute nobody logged. SI-11 is therefore less about the loop than about what the loop
must refuse to do. Designing it exposed two defects in the single-send path from SI-04, which a bulk
button would have turned from unlikely into certain.

## Decisions

### 1. Defect: nothing spaced one reminder from the next (fixed)

The ladder's steps are spaced from the **due date** (`days_after_due`: 7, 21, 35), not from each
other. So an invoice 40 days overdue that nobody had chased would get step 1, and pressing send again
would immediately send step 2, then the formal notice - every step's day had already passed. A
bulk chase retried once would have done that to every customer.

`MIN_DAYS_BETWEEN_REMINDERS = 7` refuses it (`Blocker.TOO_SOON`): a reminder is not sent within a week
of the last one about that invoice. It needed a new fact - the day of the last reminder - so migration
0055 adds `last_sent_on` to `invoicing.overdue_invoices`. It is checked after "step not due" and
"ladder complete" (so those still answer as themselves) and before the interest-rate check (a customer
reminded yesterday is "too soon", not also refused for a rate).

This is also what makes **repeating a bulk call safe**: anyone reminded a moment ago is `too_soon`.
An end-to-end test through the real database asserts a second call sends nothing.

### 2. Defect: a provider refusal rolled back its own record (fixed)

`DunningService.send_next` raised `ReminderNotDelivered`; the route turned that into a 502; and the
request's transaction (`get_db_session` uses `session.begin()`) rolled back on the exception - taking
the failed dispatch's `invoice_delivery` row and its FAILURE audit entry with it. Nothing had been
e-mailed, so the 502 was honest, but nobody investigating could see the attempt.

The dispatch-and-record logic is now `_dispatch_and_record`, which **returns** whether the provider
accepted (it does not raise), and the single-send route **returns** the 502 as a `JSONResponse`
instead of raising it, so the transaction commits with the record. A test through real HTTP against
Postgres asserts the audit entry and the dispatch row both survive the 502.

### 3. A formal notice is never sent by "chase everything"

It claims interest and collection cost. A bulk click must not be able to demand money from customers
nobody looked at. With no `items`, only friendly and ordinary reminder steps are sent; an invoice whose
next step is a formal notice is reported as `needs_confirmation`, with a message saying how to send it.
Naming the invoice in `items` **is** the explicit confirmation.

### 4. A selection carries the step the person reviewed

`items` is `[{invoice_id, step_position}]`. Every invoice is re-assessed at send time; if the step that
would go is not the one reviewed (a preview went stale, somebody else sent it, a request was retried)
the invoice is skipped as `step_changed` and nothing is sent. The person who was shown step 1 is never
sent step 2 on the strength of a click made before it changed. A step that has changed is reported as
such even when it is also blocked, as the more useful thing to say; an invoice with no step at all
(paused, paid) is simply `blocked`.

### 5. One failure does not stop the rest

Each invoice runs in its **own SAVEPOINT** (`DunningRepository.savepoint()`). A missing address, a
missing PDF, an unavailable channel or the already-sent race is caught and reported per invoice; a
database error part-way would otherwise poison the whole request transaction and lose every reminder
already recorded. A provider refusal is *returned*, not raised, so its savepoint commits and the
failed dispatch's record survives. Because a savepoint rollback also discards that invoice's own audit
entry - and in the already-sent race the customer may in fact have been e-mailed - the failure is
audited **outside** the savepoint, so no attempt leaves no trace.

The response is always HTTP 200 with a per-invoice report and a summary
(`sent / skipped / failed / deferred`). Every reason has a sentence in both languages: the blocker's
for `blocked`, and dedicated ones for each skip and failure.

### 6. A cap per call, deferring rather than dropping

At most `MAX_CHASE_PER_CALL = 50` are sent per call: sending is sequential (one SMTP hand-over each),
so an unbounded call would run past any request timeout with half the customers mailed and no report.
The rest are `deferred` and reported; calling again is safe (section 1).

### 7. Permission and audit

`send sales_invoice`, as sending one reminder (ADR-012, no new permission). Each send is audited as in
SI-04, and one `chase_overdue` summary entry records the counts and whether a person chose the invoices
or asked for everything - the audit log can tell a deliberate selection from a blanket click.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Send formal notices too, with a confirm dialog on the client | The API is the control (rule 3); a client-side dialog is presentation. |
| Preview token instead of `step_position` | State to store and expire, for what a step number already says. |
| Stop at the first failure | One wrong address would block every other customer's reminder. |
| No savepoints; rely on the request transaction | One integrity error loses every reminder already recorded. |
| Run the whole chase as a background job | No queue exists to drain one (ADR-071); a request with a cap is honest today. |
| Space reminders by making the ladder steps relative to the previous step | Changes what `days_after_due` means for every configured ladder; a floor is additive. |
| Raise on provider refusal and accept the rollback | Loses the record of an attempt an operator will later need. |

## Consequences

**Easier.** "Chase" is one call; the report says precisely what went, what did not and why. The spacing
floor protects single sends as well.

**Harder.** More than 50 overdue invoices take several calls. The 7-day floor is a constant, not a
per-administration setting.

**Known gaps.**

- **No scheduler and no background sending.** Chasing is still a person's click; there is no daily
  automatic run, and no queue to make a very large chase resilient to a timeout.
- **`MIN_DAYS_BETWEEN_REMINDERS` is fixed at 7**, and is not yet configurable per administration or
  per step.
- **A concurrent double-send can still e-mail twice** before the database records once: the constraint
  guarantees one *record* and cannot un-send. The failure is audited and reported as `already_sent`.
- **No dry run.** The overview (`GET .../dunning`) is the preview; there is no "what would this send"
  that applies the formal-notice and cap rules, so a client reproduces them from the overview's data.
- **The legal constants of ADR-071 still apply and still need review** before a real formal notice.
- **Verified against the native local Postgres** (migration 0055 applied): 30 dunning DB tests plus 9
  end-to-end tests through real HTTP, routes, services and audit log with only the mail hand-over
  faked - including the two defects above. Whole suite with DB tests on: 3299 passed, 0 failed.
