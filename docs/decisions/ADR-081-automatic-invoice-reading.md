# ADR-081: Captured invoices are read by Claude on Vertex AI (EU), behind an adapter that is off by default

- **Status**: Accepted (built; not switched on anywhere yet — see "Before it is enabled")
- **Date**: 2026-09-22
- **Serves**: FR-EXP-001c (fields pre-filled, always editable, never blocks), FR-AP-002 (extraction
  of supplier, number, date, amounts, with per-field confidence)
- **Constrained by**: PRIV-010, PRIV-011 (EU only, no sub-processor outside it), PRIV-015 (no
  training on customer data), PRIV-016 (sub-processor list, 30 days' notice), CLAUDE.md
  non-negotiable 4 (integrations sit behind adapters)
- **Builds on**: [ADR-062](ADR-062-railway-hosting-and-non-azure-cloud-services.md) (Google Cloud
  KMS in `europe-west4`, so a Google Cloud service account already exists in production),
  [ADR-031](ADR-031-receipt-capture.md) (capture), [ADR-032](ADR-032-expense-form.md) (the form)

## Context

Capturing an invoice used to end with an empty draft, and a person typing the supplier, date,
amount and VAT off the document. The product wants those read for them. Two things make that a
decision rather than an implementation detail:

1. **It sends a customer's invoice to a model.** An invoice names a supplier, an amount and often a
   bank account. PRIV-010/011 keep customer data, and every sub-processor that touches it, inside
   the EU. Anthropic's own API is not EU-resident by default, so it is not the answer without a
   documented DPO exception.
2. **The document is untrusted input.** A model that reads an invoice can be told things by it.

The API has no PDF or image library and no OCR, and `uv` is not installed on the development
machines, so a change to `uv.lock` cannot be made reliably. Reading needs to work without new
dependencies.

## Decision

**Read invoices with Claude served from Google Vertex AI in an EU region (`europe-west4` by
default), through an `InvoiceExtractor` adapter, off unless a deployment turns it on.**

- **Why Vertex.** The same Google Cloud relationship and service account that Cloud KMS already
  uses (ADR-062); EU-region serving; Claude reads PDFs and photographs natively, so there is no OCR
  step and no image library. No new sub-processor beyond the one already in the register, subject
  to the checks below.
- **Why an adapter.** `api.expenses.extraction.ports.InvoiceExtractor` is the seam (non-negotiable
  4). `VertexClaudeExtractor` is the one implementation; a different provider replaces it without
  touching capture or the form. It talks to Vertex over `httpx` with a service-account token signed
  by `PyJWT` — both already dependencies, so `uv.lock` is unchanged.
- **Off by default.** `EXTRACTION_PROVIDER=none` reads nothing. A deployment opts in with
  `vertex-claude` and a project id. A `vertex-claude` selection with no project is logged and
  treated as off rather than failing captures.
- **Where it runs.** In the request that stores a *new* receipt's first page, after the document is
  stored and the draft expense exists. A failure of any kind — provider down, missing credentials,
  a 30-second timeout, an unreadable answer — leaves the plain draft and records why. The capture
  never fails because a reading did (FR-EXP-001c). HEIC is not sent: no provider here reads it.
- **The reading goes through the form.** Fields are written with `ExpenseFormService.update`, so
  VAT is derived by the one implementation of that rule from the treatment and the invoice *date*
  (CMP-014). A single printed 21% or 9% sets the treatment; 0% does not, because it could be
  `btw_0`, `btw_vrijgesteld`, `btw_verlegd` or an export, and the invoice cannot say which.
- **A reading is checked, not believed** (`extraction.model.parse_reading`): the model is forced
  through one tool with a fixed schema; every field is parsed and range-checked; a failing value is
  dropped, never repaired; amounts are `Decimal` from a plain decimal string (NFR-031), and a JSON
  number or `1.240,00` is refused; dates outside 2000 → +1 year are dropped; text is stripped of
  control characters and capped. The system prompt says the document is untrusted and to ignore
  instructions in it. The reading only pre-fills a *draft* a person reviews.
- **What is stored.** `expense.invoice_number` (a real field) and `expense.extraction` (jsonb: status,
  provider, model, a confidence per field the reading wrote — **never the values**, which are the
  columns). The audit entry `extract_invoice` names the fields and never their content.
- **Nothing from the request or response is logged**, and a provider error body is not carried into
  the error (it can echo the request, and the request is the invoice).
- **Confidence informs review only.** The form shows "Check this" beside a field the reading wrote
  with confidence below 0.8. Nothing is blocked on it.

## Consequences

- Capturing a PDF or JPEG/PNG with reading on takes as long as the model call (bounded at 30 s) and
  holds the request's database transaction open for that time. Acceptable at current volume; if it
  is not, reading moves to a background job that opens its own tenant-scoped session, and the
  adapter, checks and storage above do not change.
- `api.expenses.extraction` is the one place in `api.expenses` allowed to reach a model. The manual
  path guard (`tests/expenses/test_manual_path.py`) now asserts that the domain modules do not import
  it, that only `routes.py` wires it, and that it is off by default.
- One more request-scoped service in `capture_page`; no new endpoint, so no new isolation surface.
  It runs on the same tenant-scoped session as the capture, and reads and writes only the expense of
  the item that was just stored. The endpoint's existing isolation coverage applies; the
  Postgres-backed suite was not run when this was written (no Postgres access on the development
  machine), and migration 0063 was likewise not applied locally.
- The category chosen at upload is unaffected: the reading never overwrites it.

## Before it is enabled

These are not done by this change and are the deployer's to confirm:

1. **Vertex AI API is enabled** in the Google Cloud project, and the service account in
   `GCP_SERVICE_ACCOUNT_JSON` has `roles/aiplatform.user`.
2. **Access to the Claude model is granted** in Vertex's Model Garden for the project, and the model
   id in `EXTRACTION_MODEL` is offered **in the chosen region**. Model ids and regional availability
   change; the default (`claude-haiku-4-5@20251001`, `europe-west4`) was not verified against a live
   project.
3. **The data-processing terms** for Claude on Vertex are confirmed to give PRIV-011 (EU only) and
   PRIV-015 (no training on customer data), and the **sub-processor list is updated** with the
   30 days' notice PRIV-016 requires.
4. `EXTRACTION_PROVIDER=vertex-claude` and `EXTRACTION_GCP_PROJECT` are set on the service.

Until then the product behaves exactly as it did: invoices arrive empty and are filled in by hand.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Anthropic API directly | Processes outside the EU by default; PRIV-010/011 would need a documented exception. |
| Local text-layer parsing plus rules | Needs a PDF dependency `uv.lock` cannot be regenerated for here; reads digital PDFs only (no photographs or scans); far less accurate on supplier and totals. |
| OCR (Tesseract) plus rules | A system binary in the image, an image library the project avoids (ADR-044's posture), and still rules on top. |
| Read in a background job | Right eventually (see Consequences) but needs a tenant-scoped session outside a request, which does not exist yet. Deferred rather than built speculatively. |
