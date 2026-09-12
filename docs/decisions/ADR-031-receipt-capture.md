# ADR-031: Receipt capture, and the two kinds of "several"

- **Status**: Accepted
- **Date**: 2026-09-04
- **Implements**: FR-EXP-001, FR-EXP-001a (PRD §6.7)
- **Serves**: FR-EXP-001c (never blocks on extraction), FR-EXP-001d (retained as a source
  document under §6.10), FR-EXP-001e (payment method captured at entry)
- **Constrained by**: SEC-005 (upload controls), FR-DOC-001 (originals unaltered), NFR-031
  (no floating point), IAM-005, IAM-090, ADR-012 (no invented permissions)
- **Related**: [ADR-030](ADR-030-document-storage.md) — the archive every captured image lands
  in; [ADR-022](ADR-022-ledger-bounded-context.md) — why capture posts nothing

## Context

> **FR-EXP-001.** Receipt capture by **camera** (single tap from the home screen, multi-page, auto
> edge detection, deskew, glare and blur warning with retake prompt) and by **upload**
> (drag-and-drop or file picker, accepting JPEG, PNG, HEIC, PDF and multi-page PDF). Both paths
> land in the same place.
>
> **FR-EXP-001a.** Batch capture: photograph or upload several receipts in one session, each
> becoming a separate expense, with a review list before posting.

## Decision

### 1. The two "several" are different, and the schema cannot confuse them

Both requirements say *several*, and they mean opposite things:

| | Means | Produces |
|---|---|---|
| **Multi-page** (FR-EXP-001) | several images, **one** receipt | **one** expense |
| **Batch** (FR-EXP-001a) | several receipts | **several** expenses |

Get them the same way round and a three-page invoice is claimed three times, or an afternoon's
receipts collapse into one. Both are silent — the numbers look plausible either way.

So the schema names three levels and `expense` hangs off the middle one with a `UNIQUE`
constraint:

    capture_session   one sitting
      capture_item    one receipt  ─── UNIQUE ──▶ expense
        capture_page  one stored original

"Each becoming a separate expense" is then a property of the schema rather than of the code that
happens to write it. No code path can produce two expenses for one receipt or one expense for two.

**A multi-page PDF is one page row.** Counter-intuitive, and it follows from FR-DOC-001: a 4-page
PDF is one file, the original is stored unaltered, so it is one `document` and one `capture_page`.
Splitting it would mean storing four things the user never gave us. `capture_page` counts *stored
originals*, not sheets of paper — and either way it is one expense, which is the answer that
matters.

### 2. "Both paths land in the same place" is one method and one endpoint

There is no `capture_from_camera` and no `/camera` route. One method takes bytes; `source` is
recorded on the row and **read by nothing**. Two entry points would be two pipelines, and the
second built would be the one that forgot the malware scan.

The camera-side processing FR-EXP-001 names — edge detection, deskew, glare and blur warnings with
a retake prompt — is **client** work, deliberately. It happens before the shutter closes, where a
retake is one tap; a server judging blur could only reject an upload the person has already walked
away from.

### 3. Multi-page vs batch is an explicit parameter, never inferred

    ?item=<uuid>   absent  → a NEW receipt: new item, new draft expense
                   present → ANOTHER original for a receipt already captured

Nothing in an image says which it is. A heuristic on elapsed time or visual similarity would be
wrong *silently*, in the direction that either multiplies or merges somebody's claim. The caller
knows; the caller says.

### 4. Capture owns no format list, no scanner and no retention rule

FR-EXP-001's five formats *are* `DocumentContentType` (ADR-030), verified from the bytes per
SEC-005. Capture calls `DocumentService.upload` and takes its answer — through the same service
instance the archive endpoints use, so a captured receipt gets the same content-type check,
malware scan, hash and per-tenant encryption as a direct upload. Restating the allowlist here
would be a second one to keep in step, and the one that drifted would be the one a receipt was
refused by.

### 5. A draft expense starts empty, and finalisation is not posting

Every field but the linkage is nullable, which is FR-EXP-001c — *"the product never blocks on
extraction being available."* A receipt that has just been photographed has an amount nobody has
read yet; requiring one would make the camera path block on OCR or on typing.

Finalisation closes the **session** and stops. **Nothing is posted to the ledger by any endpoint
here.**

> **Superseded 2026-09-04 by [ADR-032](ADR-032-expense-form.md), §4.** This ADR originally said
> finalisation moved every draft to `ready`. That was defensible only while `ready` meant nothing —
> the expense fields did not exist yet. Once FR-EXP-001b and FR-EXP-001e landed, `ready` came to
> mean "the minimum has been given", and readying a freshly captured expense would release a claim
> with no amount and no payment method: one FR-EXP-002 cannot approve and FR-EXP-003 cannot pay.
> An expense now becomes ready individually, when its own form is complete. Migration 0033's
> `expense_ready_is_complete` CHECK makes the old behaviour impossible rather than merely no longer
> done.

Two refusals before anything changes: an **empty** session (somebody told "done" after
photographing six receipts cannot tell that from six failed uploads) and an item with **no stored
original** (it would become an expense resting on nothing — the state FR-DOC-003's completeness
report finds after the fact, refused while the person is still holding the receipt).

## Alternatives considered

| Option | Rejected because |
|---|---|
| Separate camera and upload endpoints | Two pipelines. FR-EXP-001 says the opposite in as many words, and the second one built is the one that forgets a control. |
| Infer multi-page from timing or similarity | Wrong silently, and in the direction that multiplies or merges a claim. |
| Split a multi-page PDF into one page per sheet | Stores four things the user never gave us; FR-DOC-001 requires the original unaltered. |
| One expense per session | Contradicts FR-EXP-001a's "each becoming a separate expense" — and makes a shoebox one claim. |
| Create the expense at finalisation rather than at capture | Leaves a window where a receipt exists and no claim does, and makes the review list unable to show what it is reviewing. |
| Require amount/date at capture | Contradicts FR-EXP-001c. The camera path would block on OCR or typing. |
| Re-verify the format allowlist in capture | A second allowlist to keep in step with SEC-005's. |
| Delete a discarded item and its documents | Those documents are inside their FR-DOC-002 retention period and are not capture's to remove. Discarded, not deleted — and the record of the decision survives. |
| A new permission for capture | ADR-012. Appendix A's "Submit expenses" already covers it: capturing a receipt *is* submitting an expense, and §8.4 gives the Expense Submitter exactly that. |

## Consequences

**Easier.** FR-EXP-001h's e-mail and share-sheet paths are new `CaptureSource` members and nothing
else — they land in the same method. Extraction (FR-EXP-001c/P1) writes the columns that already
exist. The review list is one SQL function, so a screen showing it needs no N+1 over pages.

**Harder.** A client must track which item it is adding pages to across a multi-page capture. That
is the cost of not guessing, and it is the right side of the trade.

**Known gaps, each with a trigger.**

- **FR-EXP-001g's duplicate detection is not built.** It matches on supplier, date and amount,
  which needs extraction. What *is* here is the exact byte-identical case — the same receipt
  photographed twice in one sitting, which a batch makes common and which needs no extraction. The
  review list flags it rather than refusing: two identical receipts can be legitimate.
- **FR-EXP-001b's form and the VAT/net derivation** are not built. The columns it will write exist
  (date, supplier, gross amount, VAT rate, category) plus FR-EXP-001e's payment method, because
  "each becoming a separate expense" needs somewhere for an expense to be. `gross_amount` is
  `numeric(19,2)` and `Decimal` throughout — never a float (NFR-031).
- **FR-EXP-001d's link to the posting** is half-made. The image is retained as a source document
  and reachable from the expense through its capture item; the `document_posting_link` row
  (ADR-030) needs a journal entry, which does not exist until FR-EXP-002/003 post one.
- ~~**FR-EXP-001f's offline queue** is client-side (MOB-003). The server side it needs is this
  endpoint being idempotent-keyed and order-independent, which it is.~~ **Closed 2026-09-06 by
  [ADR-035](ADR-035-offline-capture-queue.md)**, which drains into this endpoint and relies on
  exactly those two properties.
- **`position` allocation is `max + 1`** guarded by a `UNIQUE` index. Two captures racing within
  one session lose one to a unique violation, which NFR-032's idempotency key makes safe to retry
  — preferred over a sequence, which would leave gaps in a list somebody is checking against a
  pile of paper.
