# ADR-041: Invoice template model — six columns, a compliance gate, and one borrowed permission

- **Status**: Accepted
- **Date**: 2026-09-10
- **Implements**: FR-TPL-001, FR-TPL-002, FR-TPL-003, FR-TPL-004, FR-TPL-005, FR-TPL-006,
  FR-TPL-007, FR-TPL-009, FR-TPL-012, FR-TPL-013 (PRD §6.3.1)
- **Serves**: NFR-031 (no floating point in financial calculation), NFR-032 (idempotency),
  IAM-001–005 (tenancy), IAM-090 (audit)
- **Constrained by**: ADR-037 (VAT is computed per treatment group, never per line — the reason
  FR-TPL-006 is six columns here, not seven), ADR-012 (no invented permissions outside Appendix A)
- **Related**: [ADR-037](ADR-037-sales-invoices.md) — the line-item and VAT shape this inherits;
  [ADR-030](ADR-030-document-storage.md) — why a logo is not a `document` row

## Context

> **FR-TPL-001.** Logo upload (PNG, JPEG, SVG), with position (left, centre, right), size control
> and a transparent-background preview against the paper colour.
>
> **FR-TPL-002.** Typography: a curated set of at least 8 licensed, PDF-embeddable typefaces
> covering serif, sans and monospace, chosen for numeric legibility (tabular figures) and full
> Latin-1 coverage. Separate selection for headings, body and figures.
>
> **FR-TPL-003.** Type controls: size scale, weight, line height and letter spacing, exposed as a
> small number of sensible steps rather than free numeric entry, so a user cannot produce an
> unreadable invoice.
>
> **FR-TPL-004.** Colour: accent colour, text colour and background/paper colour, with an
> automatic contrast check that warns when a combination fails legibility on screen or in
> greyscale print.
>
> **FR-TPL-005.** Layout: at least 4 starting layouts (classic, modern, compact, minimal), each
> adjustable for header arrangement, logo/address block placement, line-item column selection and
> order, totals block position, and footer content.
>
> **FR-TPL-006.** Column control on line items: show, hide and reorder quantity, unit, unit price,
> discount, VAT rate, VAT amount and line total. Hiding a column never hides information the
> invoice is legally required to carry.
>
> **FR-TPL-007.** Editable content blocks: header text, intro text, payment terms, footer, and a
> free block for chamber of commerce number, VAT number, IBAN and general terms reference. Fields
> support merge tags (customer name, invoice number, due date, amounts).
>
> **FR-TPL-009.** Statutory fields cannot be removed or hidden. The designer enforces the Dutch
> invoice content requirements (FR-AR-003): removing or obscuring a mandatory field is not offered
> as an option, and the template cannot be saved in a non-compliant state. The reason is shown
> inline, not as a generic error.
>
> **FR-TPL-012.** Templates apply to the whole document family — invoice, credit note, quote,
> order confirmation, reminder and statement — so branding is set once.
>
> **FR-TPL-013.** Language of the rendered document follows the recipient, independent of the
> designer's UI language; a template holds both Dutch and English content for its text blocks.

Nothing template-related existed before this change. Two foundations were already in place and
constrain the shape below: migration 0037 (ADR-037) already settled how a sales invoice's line
items and VAT work, and Appendix A already carries `create sales_invoice` / `send sales_invoice`
with no third row for anything template-shaped.

## Decision

### 1. One row per named template, `version` for concurrency — not for re-rendering

`invoice_template` (migration 0042) holds one row per named template, with `is_default` unique per
administration (partial index) and `version` bumped by trigger on every UPDATE. `version` exists so
two people editing the same template detect each other — the same optimistic-concurrency problem
`journal_entry`-style derived columns solve elsewhere — and nothing more. FR-TPL-017's immutability
of an *issued* document does not run through this column at all: an issued invoice's stored PDF is
already frozen independently (0040), because nothing re-reads a template at render time for a
document already issued. Editing a template can never reach back into one.

### 2. Applies to the whole document family via a PARAMETER, not per-type rows

There is no `document_type` column anywhere in 0042. FR-TPL-012 wants one look, set once, applied
to six document kinds. A row per document type would mean six templates to keep in step by hand —
the opposite of "set once" — so `api.templates.model.DocumentType` is an argument a renderer takes
(which heading prints, which merge tags resolve), never a foreign key.

### 3. Six line-item columns, not seven — the ADR-037 deviation, stated explicitly

FR-TPL-006 names seven columns: "quantity, unit, unit price, discount, VAT rate, **VAT amount**
and line total." `api.templates.model.LineColumn` has six members and does not offer `VAT_AMOUNT`.

ADR-037 decision #3 already established, for `sales_invoice_line`, that VAT is computed and rounded
**per treatment group**, never per line: twelve lines of a few cents each, rounded and summed, can
differ by a cent from rounding the group total, and the group total is both what EU VAT Directive
Art. 226 requires an invoice to show and what reaches the aangifte. The consequence ADR-037 states
directly — "a line has a net amount and deliberately no VAT amount" — means there is no per-line VAT
figure anywhere in the data model for a template toggle to control. Offering the column anyway would
force a choice between two wrong renderings: a fabricated per-line split that disagrees with the
invoice's own total, or a column of blanks. Neither is "hiding a column"; both are the kind of
document FR-TPL-009 exists to prevent, from the opposite direction — inventing content, not hiding
required content.

So this is a **deliberate PRD deviation**, not an oversight: `invoice_template_column`'s CHECK
constraint enumerates exactly `quantity, unit, unit_price, discount, vat_rate, line_total`, and
`STATUTORY_COLUMNS` (the three that lock) is a subset of those six. Six is the whole model, not six
offered out of seven possible.

### 4. FR-TPL-009 is enforced three times, cheapest bypass first

1. **The designer UI** does not offer the control at all (not built in this pass — see Known gaps).
2. **`api.templates.compliance.check()`** refuses to persist a non-compliant candidate, with a
   sentence per violation (D5: what, why, action) citing Wet OB art. 35a — the same shape and the
   same citation discipline `api.invoicing.statutory` already established for FR-AR-003.
3. **0042's own CHECK constraints** refuse the row outright, no matter what wrote it —
   `invoice_template_column_statutory_visible` and `invoice_template_block_legal_identity_tags`.

Layer 3 exists because layer 2 lives in application code a future write path (a bulk import, an
admin console, a firm's push of a house template — FR-TPL-014, P1) could bypass without ever
importing `api.templates.compliance`. The database does not trust the application to have
remembered; this is the identical posture 0037's triggers take toward `api.invoicing.service`.

### 5. Preview and issue are meant to share one render function — and do not, yet

FR-TPL-008 requires "no separate preview renderer." `api.templates.rendering.
TemplatePreviewRenderer` is built as a Protocol mirroring `api.invoicing.rendering.InvoiceRenderer`
precisely so that when a real `TemplatedPdfRenderer` exists, both the preview route and invoice
issue can be pointed at the *same* instance. This pass does **not** build that renderer — see Known
gaps — so today a preview and an issued invoice are, honestly, drawn by two different pieces of
code. The seam is real; the promise it exists to keep is not yet kept.

### 6. Logos are `template_asset` rows, not `document` rows

`api.documents` (0031) is built around FR-DOC-002's seven-year, not-deletable-by-users retention.
A logo is the opposite kind of file: a business replaces it whenever its branding changes, with no
retention obligation at all. Giving `document` a logo-shaped exception would weaken that guarantee
for every other row in it, or push a special case into every reader of `document` to skip logos.
`template_asset` is a second, narrow table instead — the same "adapter per integration, not a
special case in the general one" instinct CLAUDE.md's fourth non-negotiable states for external
providers, applied here to an internal storage decision.

### 7. Template edit/save authority is `create sales_invoice` — no new Appendix A row

ADR-012 forbids inventing a permission outside Appendix A. Appendix A has no row for templates, and
this change does not add one. Instead, `api.templates.service.InvoiceTemplateService._require`
authorises against `api.invoicing.service.CREATE_INVOICE` — the literal tuple `("create",
"sales_invoice")`, imported rather than re-typed, so the two checks cannot silently drift into two
permissions that happen to read the same today. Every route in `api.templates.routes` declares the
identical `require_permission("create", "sales_invoice", ...)` dependency
`api.invoicing.routes.create_invoice` declares.

The reading this leans on: branding what a sales invoice looks like is part of what "create sales
invoice" already grants — the Invoicer role (Appendix A) can draft an invoice's *content* and, by
the same grant, can now also draft what it *looks like*. Issuing a branded document to a customer is
still gated by `send sales_invoice`, unaffected by this change, for the same reason
`api.invoicing.service`'s own docstring gives: making a claim to somebody outside the business is a
different authority from drafting one.

### 8. Contrast checking uses `float`, deliberately, against CLAUDE.md rule four

`api.templates.model.ColorScheme.contrast_warnings` is the one piece of arithmetic in this whole
change that is not `Decimal`. WCAG 2.1's relative-luminance formula is a display-only accessibility
heuristic: it has no monetary meaning, no regulatory precision requirement, and every reference
implementation of it uses `float`. CLAUDE.md rule four exists to keep floating-point error out of
money; a contrast ratio being off in the ninth decimal place changes nothing about what anybody
owes. Using `Decimal` here would not buy correctness the requirement needs, only the cost of
reimplementing `pow`.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Offer `vat_amount` as a seventh toggleable column | There is no per-line VAT amount in the data model (ADR-037). Toggling it on would print either a fabricated per-line split disagreeing with the invoice's own total, or a column of nothing. |
| A `document_type` foreign key on `invoice_template`, one row per document kind | FR-TPL-012 wants one look set once; six rows to keep in step by hand is the opposite of that, and a renderer parameter serves the same need without the duplication. |
| Free numeric entry for type scale / logo size / line height / letter spacing | FR-TPL-003 asks explicitly for "a small number of sensible steps... so a user cannot produce an unreadable invoice" — free entry is the failure mode named in the requirement. |
| A CHECK-only enforcement of FR-TPL-009, no application-level compliance module | A CHECK constraint can refuse a row but cannot explain WHY in a sentence a person acts on (D5) — it would satisfy the letter of "cannot be saved" and fail "the reason is shown inline, not as a generic error." |
| An application-only enforcement of FR-TPL-009, no CHECK constraints | Trusts every future write path to remember to call `api.templates.compliance.check()`. 0037's own triggers already reject that trust for the ledger; there is no reason to extend it here. |
| Store the logo in `document` (0031) | FR-DOC-002's seven-year, not-deletable-by-users retention is the wrong policy for a file a user replaces at will; a special case there would weaken the guarantee for every other row. |
| Invent an Appendix A row for template management | ADR-012 forbids it. "Create sales_invoice" already covers drafting an invoice's *appearance*, not only its content — see decision #7. |
| Wire `MinimalPdfRenderer` to read a template now, fully closing FR-TPL-008 | A genuinely large change to code that issues real invoices today, out of proportion to a backend-first pass; see Known gaps. |
| Return the preview PDF as a raw binary response | SEC-005's separate-origin / attachment-disposition control belongs to a dedicated download endpoint, the way `api.documents.routes` already builds one; building a second one for an ephemeral, unstored preview render is out of scope here. A JSON envelope (base64 content + violations) keeps the route consistent with every other one in this package. |

## Consequences

**Easier.** `api.invoicing.rendering`'s eventual `TemplatedPdfRenderer` has a fully-typed
`InvoiceTemplate` to read rather than raw rows, and a `TemplatePreviewRenderer` Protocol already
shaped to receive it. FR-TPL-011's future multiple-templates-per-administration needs no schema
change — `invoice_template` already supports many rows per administration; P0 simply has services
that mostly return one. FR-TPL-014's firm-pushed house templates (P1) can reuse the same tables with
an ownership/lock field added later, without touching the six-column or five-block shape.

**Harder.** The template designer now has a second permission surface (via `CREATE_INVOICE`) whose
meaning is implicit rather than named — a future reviewer checking "who can edit a template" has to
know to look at the sales-invoice-drafting grant rather than at a dedicated row. `version`-based
optimistic concurrency means a client has to carry the version forward through an edit session and
handle a 409 by re-fetching, rather than a plain overwrite.

**Known gaps, each with a trigger.**

- **The actual PDF drawing for templates' columns, blocks, logo, typography and colours is NOT
  wired into `MinimalPdfRenderer`.** That renderer is untouched by this change, still draws one
  hardcoded layout with no branding, and is what issues every real invoice today. This migration and
  `api.templates.model`/`compliance`/`repository`/`service` exist and are fully exercised by
  `POST/PUT .../invoice-templates`; nothing outside the new routes calls them yet. Trigger: building
  `TemplatedPdfRenderer` and pointing `api.invoicing.routes.get_invoicing_service`'s
  `build_invoice_renderer` at it.
- **FR-TPL-008's preview does not yet share one engine with issue.** `api.templates.rendering.
  MinimalTemplatePreviewRenderer` is a real, working renderer — not a stub that raises — built from
  the same `api.invoicing.pdf` primitives `MinimalPdfRenderer` uses, and it draws the template's
  actual layout name, content blocks (with the two locked merge tags resolved against the real
  administration) and visible column order. It does **not** apply the template's chosen fonts (this
  PDF writer has only the two base-14 faces every invoice on the platform draws with today — the
  same FR-TPL-002/FR-TPL-020 licensing gap `api.invoicing.rendering`'s own docstring names) or its
  chosen colours (`api.invoicing.pdf.Text` carries no colour channel). Trigger: same as above —
  `TemplatedPdfRenderer` replaces this module entirely.
- **The designer UI itself is a separate piece of work.** Nothing in `apps/web` was touched. Trigger:
  a follow-up ticket scoped to the frontend, consuming the routes this ADR documents.
- **Logo upload has no endpoint yet.** `template_asset` and `invoice_template.logo_asset_id` exist,
  and `invoice_template_logo_same_tenant()` (0042) verifies a referenced asset belongs to the same
  administration, but there is no `POST` to create a `template_asset` row, and therefore no working
  FR-TPL-018 sanitisation pipeline (SVG stripped of scripts and external references) either. A
  client can set `logo_asset_id` only to `null` today. Trigger: an upload endpoint, scoped to this
  ticket's follow-up, with the sanitisation pipeline FR-TPL-018 requires before `sanitized` may be
  set true.
- **There is no `unit` column on `sales_invoice_line`.** FR-TPL-006 names "unit" as a toggleable
  column, and `LineColumn.UNIT` exists so the template model matches that list, but migration 0037
  never gave a line item a unit field (hours, pcs, kg) to display. Toggling the column on has
  nothing to show yet. Trigger: adding a `unit` column to `sales_invoice_line` — a separate,
  invoicing-side migration, out of this ADR's scope.
- **`DATABASE-backed tests are unexercised in this environment (no Postgres).** `tests/integration/
  test_invoice_template_isolation.py` is written and registered against all five routes (so
  `test_isolation_coverage.py` passes at collection time), but only *executes* under
  `TENANT_ISOLATION_TESTS_ENABLED=1`. That means the following are **unverified** here: 0042's
  `invoice_template_child_same_tenant()` and `invoice_template_logo_same_tenant()` triggers, the
  `invoice_template_bump_version()` trigger, the `invoice_template_one_default_idx` partial unique
  index, and both FR-TPL-009 CHECK constraints under a real Postgres. `tests/templates/
  test_compliance.py`'s pure-Python unit tests (no database) DO run and pass — see the pull
  request's test output. This is the identical posture ADR-037 already recorded for its own
  isolation suite, carried forward rather than improved on, because the underlying environment
  constraint (no Postgres available here) is unchanged.
