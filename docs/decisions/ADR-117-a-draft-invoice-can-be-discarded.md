# ADR-117: A draft invoice can be discarded, and a discarded one is gone from every list

- **Status**: Accepted
- **Date**: 2026-10-11
- **Serves**: FR-EXP-001a (the review list's "drop an item"), FR-EXP-001g (duplicate detection)
- **Builds on**: [ADR-101](ADR-101-invoice-number-escalates-duplicate-warnings.md) (a confirmed
  duplicate cannot be submitted), [ADR-116](ADR-116-duplicate-warning-moves-to-the-upload-and-invoice-detail-cards.md)
  (the warning is said at the upload)
- **Closes**: the gap ADR-101 and ADR-116 both recorded - no way out of a blocked duplicate

## Context

ADR-101 made one duplicate shape block submission: the same supplier, date, amount **and invoice
number**. It said plainly what it did not do: there was no way to resolve the block other than to
leave the draft sitting there. ADR-116 then made the warning arrive at the upload, which made the
gap more visible, not smaller - a person is told "this can't be submitted until this is resolved"
and given nothing to resolve it with.

The capability half-existed. `capture_item` has had `discarded_at`, `discarded_by_user_id` and
`discarded_reason` since migration 0032, `CaptureService.discard` sets them, and the review list,
the firm worklist and receipt chasing already treat a discarded item as not there. What was
missing was a route, and - the part that matters more - the other readers of `expense`.

## Decision

**`POST /v1/administrations/{id}/expenses/{expense_id}/discard`**, body `{reason}` where `reason` is
`duplicate`, `not_an_invoice` or `other`. It marks the receipt's `capture_item` discarded - the one
notion of "discarded" that already exists - and records `discard_expense` on the audit log with the
reason and the item id and nothing of what the claim said (IAM-093).

- **Only a draft.** A `ready` claim is in front of someone who books it, and a `posted` one is in the
  ledger, which is append-only (FR-GL-003, CMP-009): it is corrected by reversal, never by
  disappearing from the list it was booked from. Both are refused with 409
  `expense_not_discardable`.
- **Same permission as the form** (`submit expense`, ADR-012). Discarding a draft is deciding what
  happens to a claim you may already edit. It is not narrowed to "your own": the form is not, and a
  rule here that the form lacks would only be a rule somebody works around by editing instead.
- **The file is kept.** The original is inside its FR-DOC-002 retention period and is not the
  form's to delete; the item records who threw the invoice away and why.
- **Discarding twice is "not found"**, not a second decision: once discarded, the invoice is not
  there to be found.

### A discarded draft must stop being seen

A route that only set a flag would leave a ghost: still in the list, still blocking its twin,
still nagging. So every reader of `expense` that lists or counts drafts now leaves out an item
with `discarded_at`:

| Reader | Why it matters |
|---|---|
| `SqlCaptureRepository.get` | Every read, edit, submit and post of an expense goes through it, so a discarded invoice is "not found" everywhere at once |
| `list_by_status` | The purchases list and the mobile Approve/View tabs |
| `duplicate_candidates` | **The point of this change**: the kept invoice stops being a duplicate of the thrown-away one, which lifts ADR-101's block. Filtered around the SQL function, not inside it - it has been recreated once (0074) and is not worth a third |
| `vat_returns.unposted_purchases`, firm `unposted_in_period` | A thrown-away draft is not an unbooked purchase and must not hold up a BTW return |
| `reminders._waiting_receipts` | Nobody is waiting on a receipt that was discarded |
| Firm summary: `receipts_uploaded`, `possible_duplicates` | The accountant's morning view must not count what was thrown away |

`_TO_BOOK` (the worklist), receipt chasing (`api.chasing.missing`) and `review_list` already
filtered on it. Bank matching reads only `posted` expenses, so a draft never reaches it. No
migration: the predicate is `NOT EXISTS (SELECT 1 FROM capture_item ... discarded_at IS NOT NULL)`
on a column that already exists.

### Where it is offered

- On the **upload notice** (ADR-116): "Discard this upload", for the draft this upload just became.
  The notice is removed and the list refreshed on success; on refusal it stays and shows the
  server's sentence.
- On a **blocked duplicate invoice**, beside the sentence that explains the disabled Submit: "Discard
  this invoice", asked once more ("Discard this invoice? The file is kept."), because it removes the
  invoice from the list. Not offered anywhere nothing is blocked - a general "delete draft" is a
  larger decision than closing the gap.

## Consequences

- A blocked duplicate has a way out, and the one that was kept immediately stops showing the
  warning and becomes submittable.
- **There is no undo.** The item row and the file survive, so a restore is possible without new
  data, but no route restores it. If it is wanted it is a small, separate decision.
- **Any reader of `expense` added later must repeat the predicate.** The list above is the set
  today, found by reading every `FROM expense` in the API. A future reader that forgets it will
  show a discarded draft; the integration test that counts drafts (`test_discard_expense.py`) is
  where to extend the guarantee.
- A colleague with `submit expense` can discard a draft someone else uploaded. It is on the audit
  log with the actor and the reason, and a draft is not a booking; narrowing it to the submitter
  would also have to narrow `update`, which is a different change.

## Alternatives considered

| Alternative | Why not |
|---|---|
| A new `discarded` value of `expense.status` | The `status` CHECK, the readiness constraint and every `status IN (...)` consumer would change, and "discarded" already exists on `capture_item` where three modules honour it. Two notions of discarded would drift |
| Delete the draft's `expense` row | The item's FK, the review list's `expense_id`, and the record of who decided what would go with it. Migration 0032 chose "discarded rather than deleted" on purpose, for the documents' provenance |
| Let a `ready` claim be discarded too | It is in front of an approver; making it vanish would be a way to withdraw a claim somebody is acting on. Returning one for correction is FR-EXP-002's step, which is not built |
| Filter inside `expenses.duplicate_candidates` | The function has already needed a repair migration once. Filtering in the one caller keeps this change migration-free |
| A "confirm this is legitimate anyway" override instead | ADR-101 reasoned a real supplier essentially never reuses a number; the honest remedy for a true duplicate is to remove it, not to approve it. An override is still a possible later addition for the rare false positive |
