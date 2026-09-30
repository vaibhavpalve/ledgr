# ADR-101: The invoice number escalates a duplicate warning - one case now blocks

- **Status**: Accepted
- **Date**: 2026-10-01
- **Serves**: FR-EXP-001g (duplicate detection)
- **Builds on**: [ADR-034](ADR-034-duplicate-detection.md) (the original warn-never-block design),
  [ADR-096](ADR-096-invoice-capture-defaults-to-company-funds-and-self-submits.md)/[ADR-099](ADR-099-duplicate-warnings-hold-back-automatic-submission.md)
  (automatic submission, which already skips any duplicate warning)
- **Amends**: FR-EXP-001g's "warn, never block" - narrowly, to one specific shape

## Context

Re-uploading the same test invoice ("Mistral AI SAS", same invoice number, same everything)
produced a draft with a duplicate warning buried in the review screen - correct per ADR-034/099
(automatic submission already skipped it, per ADR-099), but the presentation was too easy to miss,
and the check itself could not tell "the same document, twice" apart from "two documents that
happen to share a supplier, date and amount" - a coincidence FR-EXP-001g's own reasoning names
explicitly (two colleagues buying the same train ticket, a subscription billed twice).

The missing signal was the invoice number. `ExpenseTriple`/`expenses.duplicate_candidates` never
compared it - FR-EXP-001g names only supplier, date and amount. But a real supplier essentially
never reuses an invoice number, so a match on all three original fields AND the invoice number is
categorically different evidence from a match on the three alone: it is not "looks similar", it is
"almost certainly the same document."

## Decision

**A new, orthogonal classification**, `DuplicateWarning.invoice_number_match` (`api.expenses.
duplicates.invoice_number_match`, pure, case/whitespace-insensitive):

- **`"same"`** - both invoice numbers present and equal. Near-certain the same document.
- **`"missing"`** - one or both sides have no number (a parking ticket, a train ticket stub).
  Nothing rules a duplicate out, so this is not the quiet case either - shown just as loudly as
  `"same"`, but does not block.
- **`"different"`** - both present and unequal. About as good evidence as exists that these are
  two real documents. The ordinary, quiet case this warning has covered since ADR-034.

This is independent of `strength` (which is still about the SUPPLIER - exact or pg_trgm-similar).
The SQL side (`expenses.duplicate_candidates`, migration 0073) only supplies each candidate's raw
`invoice_number`; Python decides what a match, mismatch or absence means, the same split ADR-034
already drew for the supplier rule (`is_exact_match` in Python, `similarity()` in SQL).

**`"same"` is the one exception to "warn, never block"**, enforced in two places, not one:

- `ExpenseView.can_be_marked_ready` (`api.expenses.form`) - blind to every warning except this one,
  via the new `has_confirmed_duplicate` property. This is what a client's submit button is
  disabled on.
- `ExpenseFormService.mark_ready` and `ExpensePostingService.post` **both** check it directly and
  raise `ConfirmedDuplicateExpense` (409) - not only the view property, because `post` can confirm
  a complete DRAFT in one step, bypassing `mark_ready` entirely (its own long-standing docstring).
  A property that only disables a button is a UI courtesy; the actual guarantee lives in the two
  places that write.

**The web form shows it before anyone has to read carefully.** `"same"` and `"missing"` both get
the same loud, red styling (`--ledgr-attention`, already in the design tokens, previously unused
in this screen) instead of the default notice look; `"same"` additionally shows an explicit
sentence naming why submission is blocked. `"different"` keeps today's quiet presentation.

## Consequences

- Re-uploading a genuinely identical invoice can no longer be submitted at all until the person
  resolves it (discard the new capture, or confirm it is legitimate some other way this ADR does
  not build) - a real behaviour change from "warn only," scoped as narrowly as the evidence allows.
- A receipt with no invoice number (most parking, most small cash purchases) now warns loudly on
  every supplier/date/amount coincidence, where before it looked identical to any other warning.
  This is deliberately noisier for exactly the case where nothing else can rule a duplicate out.
- **What this does not do**: there is still no way to resolve a blocked, confirmed duplicate other
  than discarding the new capture - no "confirm this is legitimate anyway" override exists. If a
  real false positive shows up (two genuinely different documents a supplier numbered identically,
  which should not happen but is not impossible), that is the next gap to close, not something
  this ADR already covers.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Fold invoice number into `ExpenseTriple` and `is_exact_match` | The PRD names exactly three fields for FR-EXP-001g's match rule; changing what counts as "the same claim" would also change every existing EXACT/PROBABLE classification, not just add a new one. Keeping it a separate, orthogonal signal changes nothing about the existing rule. |
| Only check in `ExpenseView`/the client, not `mark_ready`/`post` | Would make the block a UI suggestion, not a guarantee - exactly the class of gap this codebase already treats seriously (`ExpensePostingService.post()`'s own docstring already anticipates bypassing `mark_ready`). A disabled button is not enforcement. |
| Reuse `--ledgr-caution` (the existing "read failed" amber) instead of a new class | Would make a confirmed duplicate look like the unrelated "extraction had trouble" notice. `--ledgr-attention`/`--ledgr-attention-wash` already exist in the design tokens for exactly this "more serious than caution" case (already used for blocked uploads) and needed no new token. |
