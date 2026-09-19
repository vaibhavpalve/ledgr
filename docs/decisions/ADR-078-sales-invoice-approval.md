# ADR-078: Draft - approve - send for sales invoices

- **Status**: Accepted
- **Date**: 2026-09-21
- **Implements**: SI-16 (Could) - a bookkeeper drafts on a client's behalf; the business owner approves
  before it sends. Fits the existing firm-engagement access model.
- **Builds on**: [ADR-037](ADR-037-sales-invoices.md) / [ADR-039](ADR-039-invoice-posting.md) (issuing is the
  point of no return), [ADR-012](ADR-012-appendix-a-as-source-of-truth.md) (the role catalogue), IAM-060..065 (segregation of
  duties)
- **Constrained by**: CLAUDE.md rules 1-4 (in particular rule 3: one authorization library), NFR-044, IAM-005

## Context

A firm's bookkeeper holds `create` and `send sales_invoice` on a client's administration, so today they can
draft AND issue - number, post and send - an invoice the client's owner never saw. Some owners want a second
pair of eyes on what goes out under their name.

## Decision

1. **The gate is at issue.** Issuing allocates the gapless number, freezes the VAT, posts the ledger entry and
   stores the PDF; sending can only follow. `InvoicingService.issue` consults an optional `IssueGate` before the
   statutory check, so a refusal burns no number and every route to issuing meets it (the invoice route, a
   recurring schedule's `auto_issue`, whatever comes next).
2. **Opt-in, per administration.** `administration.invoice_approval_required`, default false. With it off nothing
   changes. It is set through the existing administration PATCH, which needs the organization-level
   `manage administration` authority, so a bookkeeper cannot switch the check off.
3. **With it on, an issue needs one of two things**: the actor holds `approve sales_invoice` (the owner's own
   authority - an owner drafting their own invoice is not stuck waiting for someone to approve it), or an
   approver has approved a request for exactly this draft as it is now.
4. **An approval is of a version.** `content_fingerprint` is a SHA-256 over what the customer would receive:
   the customer snapshot, dates, notes and every line, canonicalised (decimal scale and line storage order do
   not matter). An approval stores the hash it was given. Edit the draft afterwards and the hashes differ: the
   approval stops counting (state `stale`), approving a request whose draft has changed is refused, and a new
   request is needed. Without this, a small invoice could be approved and then edited into a large one.
5. **`approve sales_invoice` is a new extension capability held by the Owner alone.** It is not an Appendix A
   row (the same status as `manage customer` or `view vat_return`), so the PRD conformance test is unaffected;
   it is added to the matrix, the generated catalogue (0010 is regenerated in place) and, idempotently, to
   migration 0060 so a database that predates it gets the permission and the Owner grant. Accountant and
   Bookkeeper deliberately do not hold it: the person who drafts cannot release. Requesting approval is the
   drafter's `create sales_invoice`.
6. **A request moves once.** pending -> approved | rejected | superseded, approved -> superseded. A new request
   supersedes an earlier pending or approved one; there is at most one pending request per draft (partial unique
   index). Rejection needs a reason, kept in the record but not the audit log. The row is otherwise immutable,
   with no DELETE grant, and the database refuses a request for anything but a draft.
7. **Requesting is refused for a draft that could not be issued anyway** (the statutory gate), so the owner is
   never asked to approve something that cannot go out.

## Consequences / out of scope

- **No notification.** The owner sees a queue (`GET .../sales-invoice-approvals`) but nothing tells them it has
  grown; mail / push belongs with the notification work.
- **Credit notes are outside the gate.** `InvoicingService.credit` is a separate path that issues a credit note
  directly; a bookkeeper could still credit an invoice. Gating it is a small follow-up if wanted.
- **No approval thresholds or multi-step chains** (e.g. only above an amount). The gate is all invoices or none.
- **Delivery is not separately gated.** Once issued, sending is the existing flow; the gate is the release.
- The Owner is the only approver. A firm-run client whose owner never logs in has nobody to approve; that client
  should leave the policy off or give an approver role the capability (custom roles can already carry it).

## Found while building it

The end-to-end test issued a real invoice through the API for the first time and found that **rendering the PDF
failed for any invoice read from the database**: quantities, prices and discounts are stored at four places
(`10.0000`) and rates at three, while rendering formatted them at a fixed scale of two, and the formatter refuses
to show a value at fewer places than it carries. Every rendering test built its decimals by hand, and the
recurring-invoice test tolerated an issue error, so nothing saw it. Fixed in `api.invoicing.rendering`
(`_trimmed` / `_number` / `_price`: shown at the value's own scale, never rounded) with a regression test.