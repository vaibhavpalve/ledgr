# ADR-067: EPC069-12 "pay by bank" QR code on invoice PDFs

- **Status**: Accepted
- **Date**: 2026-09-19
- **Implements**: SI-02 (TRD, "Quick wins" table) - a scannable QR code on an issued sales
  invoice that pre-fills a SEPA credit transfer in the customer's own banking app.
- **Serves**: NFR-031 (no floating point in the calculation path - the QR carries the invoice's
  already-computed `gross`, never recomputes it).
- **Constrained by**: CLAUDE.md non-negotiable #4 (integrations behind adapters, applied here by
  analogy: the QR is a data format, not a provider, so no adapter is needed - see "Alternatives
  considered"), ADR-042 (the one `TemplatedPdfRenderer`/`api.invoicing.pdf` engine this must render
  through, not a second PDF path), ADR-044's "no image library in this dependency set" precedent
  (the same posture, applied to QR encoding).
- **Related**: [ADR-042](ADR-042-invoice-pdf-rendering.md) - the renderer and hand-rolled
  `api.invoicing.pdf` this places the QR image into; [ADR-044](ADR-044-logo-upload-and-svg-sanitization.md)
  - the prior "hand-roll or pick a minimal dependency, never pull in an image pipeline" decision
  this follows the same reasoning as.

## Context

SI-02, from the sales-invoicing brainstorm: a customer receiving an invoice should be able to scan
a QR code with their own banking app and have the transfer pre-filled - IBAN, amount, reference -
rather than typing it in by hand. The European Payments Council's EPC069-12 ("Quick Response Code
Guidelines to Enable Data Capture for the Initiation of a SEPA Credit Transfer") is the standard
every mainstream Dutch and wider-SEPA banking app already reads for this: a twelve-line,
newline-separated, POSITIONAL text payload, turned into a QR code image.

This is a SEPA credit transfer **initiation** the customer's own bank executes - LEDGR never
touches the money, never learns whether or when the transfer happened, and there is no
"did they pay" signal produced by this feature. That is a different, larger feature (FR-AR-009:
payment links via a PSP - iDEAL, card, direct debit - with its own account, fees and reconciliation
against LEDGR's own ledger). SI-02 is deliberately scoped to not be that: no PSP, no account this
deployment holds money in, no per-transaction fee, no reconciliation signal. A supplier who wants
FR-AR-009's payment tracking still gets it, separately, later; a supplier who just wants their
IBAN on the invoice as a scannable code gets it now, for nothing.

## Decision

### 1. The payload's exact shape is the point - `api.invoicing.epc_qr`, tested against the spec

`build_epc_payload()` produces the EPC069-12 (GUF version 002) text block field-for-field, in the
standard's own order (Service Tag, Version, Character set, Identification, BIC, Name, IBAN,
`EURnnnn.nn`, Purpose, structured/unstructured remittance, originator info). Getting a field's
POSITION wrong - not its content, its position - produces a QR code that still scans, into a
rejected or wrong transfer, so the module's tests (`tests/invoicing/test_epc_qr.py`) pin line
position, not just presence. Control characters in a name or reference are stripped rather than
rejected, since they would otherwise inject an extra "line" and shift every field after it. Trailing
empty fields (BIC, Purpose, unused remittance fields) are dropped per the standard's own guidance -
a smaller, more reliably scannable code at invoice print size.

The module raises `ValueError` for an invalid IBAN or a non-positive amount, checked HERE rather
than left to the caller: a caller that got either wrong would otherwise ship a QR code requesting a
nonsensical transfer with no indication anything was wrong.

### 2. IBAN validation lives at `api.iban`, not under either package that uses it

The supplier's own IBAN is written once (onboarding: `PATCH /v1/administrations/{id}`) and read
back once per invoice render (invoicing: `rendering.py`). Both packages already exist and must not
depend on each other (`api.onboarding` and `api.invoicing` are separate bounded contexts by
convention throughout this codebase). `api.iban.parse()` - a dataclass wrapping a checksum-verified
IBAN, ISO 7064 MOD 97-10, hand-rolled against the standard library - sits at the top level
(`api.iban`, not `api.invoicing.iban` or `api.onboarding.iban`) for exactly the reason
`api.customers.vat_number` does NOT need this treatment: a VAT number is read and written only
within `api.customers`, but an IBAN's write side and read side are genuinely in different packages.

Validation happens at WRITE time (the `PATCH` handler), not at read time or via a database `CHECK`
constraint: a `CHECK` constraint would have to duplicate the checksum algorithm in SQL, a second
place for it to drift from the Python implementation, for a value that is only ever produced by one
write path.

### 3. QR image encoding: `segno`, the one new runtime dependency this ADR adds

No pure-Python, zero-dependency QR encoder exists in the standard library, and this codebase's
established posture (ADR-044, and `api.invoicing.pdf`/`api.invoicing.truetype` before it) is to
hand-roll a narrow, well-understood format rather than pull in a library - but a QR code's error-
correction and matrix-placement algorithm (Reed-Solomon coding over multiple format versions) is
not "narrow" in the way a fixed binary format like PDF or TrueType is; hand-rolling it here would
be re-implementing a real spec for no benefit over a small, focused library that already does.
`segno` was chosen over the more commonly seen `qrcode` package specifically because `qrcode`
depends on Pillow for anything beyond raw matrix output - reintroducing the exact "image library in
the dependency set" this codebase has twice now deliberately avoided (ADR-044). `segno` has zero
runtime dependencies and writes PNG directly.

`render_epc_qr()` confirms (and is tested against) the actual bytes `segno` produces: 1-bit
`/DeviceGray`, non-interlaced PNG - exactly the shape `api.invoicing.pdf.load_image()` already
parses for a logo, with no new code needed in the PDF writer itself. The QR image is placed via the
same `Page.place_image()`/`Figure` structure-element path a logo uses, including PDF/A alt text
(`invoice.pdf.qr_alt`) for the same accessibility reason ADR-044's logo alt text exists: a reader
who cannot see the image still needs to be told what it is.

### 4. Rendering gate: `supplier.iban is not None` AND not a credit note

`rendering.py`'s `_lay_out()` renders the QR only when the supplier has an IBAN on file (the
ordinary state today - the field is new and optional, `None` by default) and the document is not a
credit note. A credit note reduces or refunds what the customer owes; a QR code requesting a SEPA
transfer FROM the customer would be actively wrong on that document, not merely superfluous.
`build_epc_payload` would in fact refuse a non-positive amount on its own, but the gate sits at the
call site, before `epc_qr` is ever reached, so the "why" is legible at the point that matters rather
than discovered as a raised exception three calls deep.

A `ValueError` from `render_epc_qr` (which should be unreachable given the gate above, since the
IBAN was already validated at write time) is caught and swallowed rather than failing the whole PDF
render: an invoice that would otherwise be perfectly issuable should not fail to render over a QR
code that is a genuine bonus, not a required statutory field art. 226 does not ask for.

### 5. The IBAN travels through `GET /v1/me` the same way `vat_number` already does

`administration.iban` is threaded through the same three call sites `vat_number` already goes
through - `api.account.routes._owned_administrations`, `_administration_details`,
`administration_entry_json` - because it needs to reach the settings screen the same way any other
editable administration field does, and a fourth, IBAN-specific code path would only be a second
place for that shape to drift from `vat_number`'s already-proven one.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A PSP-backed payment link (iDEAL/card/SEPA DD) instead of/alongside a QR | That is FR-AR-009, a materially larger feature (an account to hold funds, fees, reconciliation against the ledger) that this pass is not scoped to build. SI-02 is specifically the zero-cost, no-PSP option. |
| `qrcode` (the more commonly seen Python QR package) | Depends on Pillow for anything beyond a raw bit matrix - reintroducing the image-library dependency ADR-044 deliberately avoided for the identical reason (a narrow format doesn't need a general imaging pipeline). |
| Hand-roll the QR matrix/Reed-Solomon encoding, matching this codebase's PDF/TrueType posture | A QR code's error-correction coding is a real algorithm, not a fixed binary format - re-implementing it captures none of the "narrow, stable spec" benefit hand-rolling PDF/TrueType has, for a well-maintained zero-dependency library that already exists. |
| A `CHECK` constraint on `administration.iban` enforcing the checksum in SQL | Would duplicate the mod-97 algorithm in a second language for a value written through exactly one code path; validate once, at that path, instead. |
| Put `Iban`/`parse` under `api.onboarding` or `api.invoicing` | Whichever package it lived under would make the OTHER package depend on it, breaking the bounded-context separation both packages already maintain elsewhere in this codebase. |
| Render the QR unconditionally, refusing the whole invoice render when the amount is non-positive | Would turn a missing bonus feature (no IBAN on file, or a credit note) into a hard failure of invoice issuance - the wrong failure mode for a feature that is additive, not required. |

## Consequences

**Easier.** A supplier who fills in their IBAN once gets a working "pay by bank" QR code on every
future invoice, with no PSP account, fee, or integration to set up - the lowest-friction version of
SI-02's brainstormed idea.

**Harder.** Nothing structurally: this is additive to the existing renderer and template model, with
no change to any existing invoice's statutory content path.

**Known gaps.**

- **No BIC is populated.** EPC069-12 has made BIC optional since 2016 (GUF version 002, used here)
  for domestic and most SEPA transfers; a deployment that later needs it for a specific corridor can
  add an `Administration.bic` field and pass it through `EpcQrRequest.bic`, already a parameter.
- **No "did they pay" signal**, by design - see Context. FR-AR-009, when built, is the feature that
  answers that question, separately and without reusing this module.
- **Structured remittance (ISO 11649) is not offered.** The invoice reference travels as
  UNSTRUCTURED remittance information only - the field a Dutch "betalingskenmerk" ordinarily goes
  in - because this deployment has no structured-reference scheme to populate the alternative,
  mutually-exclusive field with.
