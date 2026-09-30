# ADR-099: A duplicate warning holds automatic submission back; a person's own submit is unchanged

- **Status**: Accepted
- **Date**: 2026-09-30
- **Serves**: FR-EXP-001g (duplicate warnings), FR-EXP-001c/e/AP-002 (automatic submission, ADR-096)
- **Builds on**: [ADR-096](ADR-096-invoice-capture-defaults-to-company-funds-and-self-submits.md)
  (submission without a person, when VAT is certain)

## Context

ADR-096 lets a clean reading submit a claim without anyone clicking a button. FR-EXP-001g's
duplicate detection was built the other way on purpose: it warns, never blocks
(`ExpenseView.can_be_marked_ready` is explicitly blind to `duplicate_warnings`), because a
legitimate repeat purchase is ordinary and refusing it would be wrong about the money. That design
assumed a person was there to read the banner and decide.

Automatic submission removes that person. A reading that is both VAT-certain and duplicate-looking
would submit itself with nobody ever seeing the warning - only the audit log would show it was
raised, after the fact.

## Decision

`InvoiceExtractionService._submit_if_vat_is_certain` now also checks
`ExpenseView.has_duplicate_warning` and holds the claim in `DRAFT` when it is true, alongside the
existing `can_be_marked_ready` check. Everything else about FR-EXP-001g is unchanged:

- A **person's own submit button is still never gated on a duplicate warning** - the banner shows,
  they decide, same as always.
- The claim that fails this check is not refused or specially marked - it is simply left as an
  ordinary draft, which already renders the normal editable review screen with the duplicate
  banner visible, exactly the moment ADR-096 otherwise skips.

## Consequences

- A reading that is certain about VAT but looks like a repeat of an existing claim now waits for a
  person, where before it would have submitted silently.
- No new state or column: this is one extra condition in an existing check, in the one place
  automatic submission is decided.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Also gate a person's manual submit on duplicate warnings | Reverses FR-EXP-001g's own reasoning ("a system that refused them would be wrong about the money and would teach people to work around it") for a problem that only exists in the automatic path. The manual path already has the safeguard - a person reading the banner. |
| Record the duplicate but submit anyway, relying on the audit log | The audit log is read after the fact, if at all. The whole point of the banner is to put the decision in front of someone before the claim moves - silently submitting past it and hoping someone audits later defeats that. |
