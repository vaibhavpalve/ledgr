# ADR-069: Live BTW rubriek preview on sales invoices

- **Status**: Accepted
- **Date**: 2026-09-19
- **Implements**: SI-13 (TRD, "Differentiators beyond the PRD") - show which VAT return box an
  invoice will land in while it is being built, not only after filing
- **Builds on**: [ADR-027](ADR-027-effective-dated-tax-rules.md) - the effective-dated
  treatment-to-rubriek mapping this reads; [ADR-037](ADR-037-sales-invoices.md) - the invoice view
  it extends
- **Constrained by**: CMP-014 (rules are read as of the invoice date), FR-VAT-003 (a provisional
  ruleset is not a basis for a filed return), NFR-031 (no floating point)

## Context

Choosing a VAT treatment on a line is, without saying so, choosing a box on the OB-aangifte: a
standard-rate supply reports in 1a, an export in 3a. Today that consequence is discovered when the
return is prepared, weeks later. A bookkeeper who picks the wrong treatment finds out then, when
the invoice is already issued and numbered and the fix is a credit note.

## Decision

### 1. Read the mapping the return reads; keep no second copy

The preview is computed from `vat.rules_on(invoice_date)` - the same effective-dated
treatment-to-rubriek mapping (ADR-027) the return builder reads. It cannot drift from filing
because there is nothing to drift: no table, no hard-coded "btw_21 is 1a" in the invoicing
package, and a rule change takes effect on the next view. `SqlInvoiceRepository.effective_rules_on`
delegates to `SqlVatRulesRepository.rules_on` rather than restating its three queries, so there is
one reader of that function.

### 2. Ride on the invoice view; no new endpoint

`rubriek_preview` is a field of the existing view, which `create`, `PUT .../lines` and `GET` all
return. The bookkeeper edits lines as a block and saves, so every save answers with the boxes those
lines now land in: the preview is "live" at the granularity the form already works at. A stateless
`POST .../preview` taking unsaved lines would refresh on every keystroke but is another route to
authorize, isolation-test and keep in step with the real computation; it can be added later on the
same pure function if a client needs it.

It is computed for every status: a draft shows where it *will* land and an issued invoice where it
*did*, from the rules on its (frozen) invoice date. It is skipped for a draft with no lines, so an
empty form costs no rules lookup. It gates nothing - `can_be_issued` is FR-AR-003's alone.

### 3. What one row is

The form's boxes carry a turnover column and, for some, a VAT column under one code (`1a` holds
both). A row therefore has `turnover_amount` and `vat_amount`, where `vat_amount` is **null for a
box with no VAT column** (1e, 3a, 3b) - which is not the same statement as a VAT column of zero, and
a treatment that *has* a VAT box reports its amount even when that amount is zero. Rows are ordered
as the form is (numerically, then by letter: `2a` before `10a`), listing only boxes the invoice
touches, with the treatment codes that feed each.

Amounts are the invoice's own, unrounded. The return's rounding rules are not applied; this is an
invoice's contribution, not a return.

### 4. What is said out loud instead of guessed

`unplaced_treatments` lists treatments whose amounts cannot be shown in a box, for two reasons:

- **No mapping on the invoice date** - a hole in the ruleset, the same species as a missing rate.
  Dropping it would make the invoice look complete.
- **The margin scheme.** What the return reports for a margin supply is the *margin*, not the sale
  price on the invoice. Putting the invoice's figures in 1a would be a wrong number, and showing a
  zero would be a wrong statement. A client should say "cannot be placed".

### 5. Tenancy

The mapping is global reference data, not tenant data: `vat.rules_on` returns the same rows to every
tenant and reads none of theirs, so the preview adds no tenant-scoped read. What it *does* expose
about the invoice is derived from the invoice already being returned to the same caller. No new
endpoint, so the existing `GET .../sales-invoices/{id}` isolation test remains the boundary test.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Map treatments to boxes in the invoicing package | A second copy of ADR-027's mapping that drifts on the first rule change - exactly what effective-dating exists to avoid. |
| Show the preview only after issue | The whole point is to see it while the treatment can still be changed. |
| A stateless `POST .../preview` on unsaved lines | More surface for a refresh rate the form does not have yet; the pure function supports it when wanted. |
| Show margin-scheme sale price in 1a | The return reports the margin; the figure would be wrong. |
| Apply the return's rounding | The preview is not the return, and rounding rules belong to the return builder (FR-VAT-*). |
| Show VAT as `0.00` in boxes with no VAT column | Conflates "no such column" with "an empty one". |

## Consequences

**Easier.** A client renders `rubriek_preview.boxes` with one loop; titles are already in the
reader's language (Dutch when a box has no English title - the box's official name is Dutch, so the
fallback is never wrong). A wrong treatment is visible before issue.

**Harder.** Viewing an invoice that has lines costs `vat.rules_on` (three small queries). The rules
are tiny and global; if it shows up in profiles it caches per invoice date with no invalidation
problem, since a rule is append-only (CMP-014).

**Known gaps.**

- **Verified end to end on the native local Postgres.** The pure function and the service are
  tested against the *real shipped ruleset* through the in-memory rules repository, `_view_json`'s
  wire shape is asserted, and `tests/integration/test_invoice_rubriek_preview_db.py` runs the one
  production reader (`SqlInvoiceRepository.effective_rules_on`) as `ledgr_app` and feeds its result
  to `preview`, so the database's shape and the function's expectation cannot drift apart unseen.
- **The shipped ruleset is `provisional-subset`.** The preview is as authoritative as the ruleset
  loaded (FR-VAT-003); it is safe to show, not a basis for a filed return. The view does not yet
  carry that flag - the loaded rule set's `source` is not on `EffectiveRules`.
- **Boxes 2a, 3c, 4a, 4b and the 5x subtotals are not previewed**: the shipped mapping never sends
  a *sale* into them, and 5a/5b are computed from other boxes, never posted to.
- **No UI.** `packages/shared-types` carries the type; rendering is client work.
