# ADR-116: The duplicate warning moves to the upload, and a submitted invoice shows what was read from it

- **Status**: Accepted
- **Date**: 2026-10-11
- **Serves**: FR-EXP-001g (duplicate detection), FR-EXP-001 (capture), FR-AP-002 (automatic reading)
- **Builds on**: [ADR-034](ADR-034-duplicate-detection.md) (warn, never block),
  [ADR-101](ADR-101-invoice-number-escalates-duplicate-warnings.md) (the invoice number escalates,
  one case blocks), [ADR-035](ADR-035-offline-capture-queue.md) (uploads only go through the queue)
- **Amends**: where FR-EXP-001g's warning is _shown_. What it blocks (ADR-101) is unchanged.

## Context

Two complaints about one screen, the purchase invoice, both from looking at it.

**1. The duplicate warning was in the wrong place.** An invoice uploaded four times produced four
drafts, and opening any of them showed a block of four identical lines ("Mistral AI SAS · 28-09-2026
· € 10.00 · You submitted this one yourself · same invoice number") inside the form, under the
fields, in front of a Submit button. It was found by whoever opened the invoice afterwards, and said
the same thing once per matching claim. The moment it is most useful is the moment the file lands,
while the person is still looking at the upload.

That was not possible when ADR-034 was written: matching on supplier, date and amount needs a
reading, and a photograph has none (FR-EXP-001c). It is possible now. The reading runs inside the
upload request itself (`capture_page` calls `read_into_expense` before it answers), so the answer
exists in the response.

**2. A submitted invoice showed one sentence and a blank page.** When the reading is certain of the
VAT rate the invoice is submitted without a person (FR-AP-002), and its screen became "We read this
invoice automatically and submitted it. There's nothing left for you to review." beside a narrow
original, with the right half of a wide monitor empty. Everything that had been read was in the
database and none of it was on the screen. The shell also held every screen to 76rem.

## Decision

### The warning is said at the upload, once

`POST .../capture-sessions/{id}/pages` returns two more fields for a receipt's first page:
`expense_id` (the draft it became) and `duplicate` - null, or `{match, expense_id, supplier,
expense_date, gross_amount, invoice_number, status, same_submitter, count, invoice_number_match}`.
`ExpenseFormService.duplicate_notice` decides it, after the reading, in order of how sure it is:

| `match` | Meaning | Presentation |
|---|---|---|
| `same_file` | Byte-identical (`document.content_hash`) to a file already stored for this client | Loud |
| `same_invoice` | Supplier, date, amount **and** invoice number match (ADR-101's `"same"`) | Loud; says it cannot be submitted |
| `same_details` | Supplier, date and amount match; number differs or is missing | Quiet; may be a second real invoice |

`same_file` needs no reading at all, so it is caught when reading is unavailable or failed - which
the supplier/date/amount check cannot do. It is matched within one administration only
(`document_content_hash_idx` is `(administration_id, content_hash)`), and a discarded receipt is
not a duplicate of anything.

**It warns and never refuses.** The file is stored and the draft exists either way. Two identical
invoices can be legitimate (ADR-034), and a refused upload is a lost document. `same_submitter` is a
boolean and nothing more: a duplicate check is a poor place to learn what a colleague spends.

**Getting the answer to the screen.** The upload goes through the offline queue and the queue
deleted the response. The uploader now hands a delivered first page's `expense_id` and `duplicate`,
with the administration and filename from the sealed payload, to `CaptureQueue.onDelivered`. It is
an event rather than a snapshot field because "this one was a duplicate" has to be said once, to
whoever is looking, and a count going up does not say that. `TransportResponse` gains two optional
fields, so a transport that predates them is unchanged. Notices are held per queue, outside React,
so they survive opening the invoice and coming back; they are filtered by administration so one
client's duplicate is not announced on another client's screen; and they stay until dismissed.

### The invoice no longer lists its look-alikes

The `duplicate_warnings` array is still in the API response and unchanged. The form does not render
it. What it keeps is **one sentence** when ADR-101's confirmed duplicate disables Submit, because a
button that refuses without saying why is worse than a banner. An ordinary look-alike adds nothing
to the form: it was said at the upload.

### A submitted invoice shows what was read from it

Beside the original, as cards (`PurchaseDetails`):

- **Invoice data** (once submitted): supplier, invoice number, date, total, BTW and amount, net,
  category, how it was paid, status.
- **File**: name, type, size, pages, source (upload or camera), the day it was added, whether and
  how it was read.
- **History**: added, read, submitted (and whether that was automatic), booked.

A draft shows File and History but not Invoice data, because those fields are the editable form
beside it and a read-only copy of a field someone is typing into is stale by the next keystroke.

`GET .../expenses/{id}` adds `created_at`, `posted_at`, `journal_entry_id` and `original` (filename,
content type, size, page count, source, captured-at), and the extraction record exposes `read_at`
and `submitted`. No migration: every one of these is a column that already exists.

The invoice screen takes `max-width: 100rem` instead of the shell's `76rem`, and the original gets
the larger share (7fr/5fr).

## Consequences

- An invoice uploaded twice is told so in the response to the second upload. A person who uploads
  twenty invoices sees a notice for each that repeats one, with links to both.
- The invoice itself is quieter. A person who opens a blocked duplicate from the list, not from the
  notice, sees only the one-sentence reason.
- **Due date, IBAN, supplier VAT and KvK numbers and line items are not shown, because they are not
  read.** The reading extracts five fields (`api.expenses.extraction.model.FIELDS`). A row for a
  fact nobody has would be a row of dashes that reads as "we failed to show this". Extending the
  reading is its own decision: it changes the provider prompt, the validation and where those
  values live, and is the natural next step if these are wanted.
- **The history has no author and no time-of-day.** The audit log holds both, but exposing it to
  every role that can open an invoice is a read surface this change does not add, and the shared
  date formatter takes a calendar date (FR-LOC-002). A manual submission records no time at all, so
  that entry has no date rather than a guessed one.
- **There is still no way to resolve a blocked duplicate other than leaving it** - the same gap
  ADR-101 recorded. `CaptureService.discard` exists and has no route. The notice now makes the gap
  visible earlier; closing it (a discard action on the notice) is the obvious next step.
- Uploads from the receipts-needed screen (`ReceiptsNeededScreen`) embed the same capture flow but
  do not render the notices yet; they are held by the same store and need only the component.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Refuse the upload with a 409 when it is a duplicate | The document is lost, offline captures could never be refused usefully (they are delivered hours later), and ADR-034's reasoning about legitimate repeats still holds. A warning costs a glance; a refusal costs a document. |
| Return the notice from a second call after the upload | The queue deletes the record on delivery and the screen may have unmounted; a poll for a result that only exists in the response is a second, racing delivery path (ADR-035 §2). |
| Compute `same_file` only, and leave look-alikes to the form | Most re-uploads of one invoice are different bytes (re-downloaded, re-scanned). The reading-based check is the one that catches them, and it is available at the same moment. |
| Keep the list in the form as well, "for reference" | That is the complaint: the same statement made twice, once per matching claim, in front of the button. |
| Build the history from the audit log | Needs a per-resource audit read for every role that can open an invoice, a decision about who may see who acted, and a time format the catalogue does not have. The facts available without any of that tell most of the story. |
| Show empty rows for due date, cost centre etc. | They are not captured. Empty rows imply a feature that does not exist. |
