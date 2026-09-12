# ADR-042: One invoice renderer, hand-rolled TrueType embedding, and an honest PDF/A gate

- **Status**: Accepted
- **Date**: 2026-09-10
- **Implements**: FR-TPL-001 (logo, partially), FR-TPL-002 (typography, mechanism), FR-TPL-003
  (typography, mechanism), FR-TPL-004 (colour), FR-TPL-006 (columns), FR-TPL-007 (content blocks),
  FR-TPL-008 (PRD §6.3.1, corrects a violation), FR-TPL-015 (PDF/A-3, mechanism), FR-TPL-016
  (extended to new content), FR-TPL-017 (unaffected — still holds)
- **Serves**: NFR-031 (no floating point in financial calculation — untouched, this is
  presentation), CLAUDE.md non-negotiable #4 (adapters, not special cases)
- **Corrects**: ADR-041 decision #5 and its "Known gaps" — the preview/issue split that ADR-041
  named as a deliberate, temporary gap is closed here
- **Constrained by**: ADR-041 (the `InvoiceTemplate` domain model this renders), the hard
  constraint recorded below (no font binary or ICC profile may be fabricated)
- **Related**: [ADR-041](ADR-041-invoice-template-model.md) — the template model this renders;
  [ADR-037](ADR-037-sales-invoices.md) — why there is no per-line VAT amount to draw

## Context

> **FR-TPL-008.** Live preview updates as settings change, rendered from the same engine that
> produces the final PDF — what is previewed is what is sent. No separate preview renderer.

> **Q15** (Open Questions). Is PDF rendering built in-house or on a managed service? It must run
> in-region (PRIV-010) and produce byte-identical output to the live preview (FR-TPL-008).

> **FR-TPL-015.** Generated PDFs are PDF/A-3 compliant for archiving, with the structured
> e-invoice XML embedded once Peppol ships in P2.

> **FR-TPL-016.** Accessibility of output: tagged PDF structure, selectable text (never a
> rendered image), and a minimum effective body size of 9pt.

ADR-041 built the template domain model (`api.templates.model`), its compliance gate
(FR-TPL-009), and a *seam* for FR-TPL-008 — but not the substance. It shipped two real,
independently-working renderers: `api.invoicing.rendering.MinimalPdfRenderer` (one hardcoded
layout, used at invoice issue) and `api.templates.rendering.MinimalTemplatePreviewRenderer` (reads
an `InvoiceTemplate`, used only by the preview route). ADR-041 named this explicitly as a "Known
gap, with a trigger": *"today a preview and an issued invoice are, honestly, drawn by two different
pieces of code, which is exactly the gap FR-TPL-008 exists to close."* This ADR is that trigger.

Separately, `api.invoicing.pdfa`'s conformance gate has held one gap open since it was written:
`fonts_embedded` was a hardcoded `False` at `render_pdf`'s one call site, because no font-embedding
mechanism existed. `api.invoicing.pdf`'s own docstring named this as "a licensing gap... not a
coding one" — true then, and the coding gap this ADR closes without touching the licensing one.

## Decision

### 1. One renderer: `api.invoicing.rendering.TemplatedPdfRenderer`

`api.templates.rendering` (the module `MinimalTemplatePreviewRenderer` lived in) is **deleted**.
`MinimalPdfRenderer` is **replaced** by `TemplatedPdfRenderer`, in the same module, extending
`InvoiceRenderer`'s Protocol to take the template it draws:

```python
async def render(
    self, view: InvoiceView, *, supplier: SupplierDetails, formatting_locale: str,
    template: InvoiceTemplate, document_type: DocumentType,
) -> RenderedInvoice: ...
```

Both call sites now build the identical call shape:

- **Issue** (`api.invoicing.posting.SalesPostingService._store_rendering`) resolves the
  administration's `is_default` `InvoiceTemplate` (a new `SalesPostingRepository.
  default_invoice_template` method, `api.invoicing.posting_repository.
  SqlSalesPostingRepository`'s own query against `invoice_template` — duplicated rather than
  imported from `api.templates.repository`, the same cross-package posture `supplier()` already
  took in both files) and derives `document_type` from `invoice.is_credit_note` (0037's existing
  property; a renderer parameter, never a fact the template itself carries — ADR-041 decision #2).
  An administration with no saved template gets `api.invoicing.rendering.default_template()`, a
  synthetic `InvoiceTemplate` with the exact scaffold `POST .../invoice-templates` gives a
  brand-new one (`_default_columns`/`_default_blocks`), so issuing an invoice never depends on the
  designer having been opened first.
- **Preview** (`api.templates.service.InvoiceTemplateService.preview`) builds the candidate
  `InvoiceTemplate` from the request body (unchanged from ADR-041) and a **fabricated,
  deterministic** `InvoiceView` (`_sample_view` — fixed sample dates, a fixed sample customer, no
  `date.today()` or `uuid.uuid4()`, so previewing the same draft twice produces the same bytes).

Neither call site calls a renderer by name beyond `TemplatedPdfRenderer`/`build_invoice_renderer`.
`tests/templates/test_render_parity.py::test_preview_and_issue_render_byte_identical_pdfs` proves
the two real call paths (`SalesPostingService._store_rendering` and `InvoiceTemplateService.
preview`) produce byte-identical output given the same template, supplier, locale and document
type — see "Known gaps" below for the one respect in which this is a proof of the *mechanism*
rather than of "a live preview equals the invoice that gets sent five minutes later."

### 2. Hand-rolled TrueType parsing, subsetting, and CIDFontType2 embedding

New module `api.invoicing.truetype`, in the same spirit `api.invoicing.pdf`'s own docstring states
for itself: every PDF or font library available brings a rendering engine, font subsetting for
scripts this platform never draws, hinting, and (for most) an OpenType layout engine — for a
document that draws Latin text in one weight at a time. It parses `head`/`hhea`/`hmtx`/`maxp`/
`cmap` (formats 0, 4, 6, 12)/`loca`/`glyf`/`OS/2`/the four fixed fields of `post`, computes a
genuine subset (closed over composite-glyph references — an accented letter built from two
outlines cannot lose one), and builds the PDF objects for a `Type0`/`CIDFontType2`/`Identity-H`
embedding: `FontDescriptor` fields computed from the parsed tables (Flags, FontBBox, ItalicAngle,
Ascent, Descent, CapHeight — `StemV` is the one industry-standard *heuristic*, since TrueType has
no field for it), a `/W` width array, and a CID-keyed ToUnicode CMap
(`api.invoicing.pdfa.to_unicode_cmap_cid` — 4-hex-digit codes and a `<0000>`–`<FFFF>` codespace,
deliberately NOT `to_unicode_cmap`'s single-byte shape, which would silently mis-map every CID
above 0xFF).

`CIDToGIDMap` is always `/Identity`: subsetting assigns new sequential glyph ids starting at 0
(`.notdef`, always retained), and those new ids ARE the CIDs, so there is no separate mapping table
to build or get wrong.

`api.invoicing.pdf.render_pdf` gained an additive `embedded_fonts: Sequence[EmbeddedFont] = ()`
parameter. A `Text` run whose `font` names an embedded font's resource name is drawn as an
`Identity-H` hex string against a real `Type0` font; every other run keeps drawing against base-14
exactly as before — calling `render_pdf` with no `embedded_fonts` (the default, still true of every
existing call site with nothing configured) produces **byte-identical** output to before this
change (`tests/invoicing/test_pdf.py::
test_existing_base14_only_rendering_is_byte_identical_with_the_new_parameter`).

### 3. `fonts_embedded` is now computed, not asserted

`render_pdf` scans every non-empty `Text` run on the document and checks its font against
`embedded_fonts`. `fonts_embedded` is `True` only when **every** font actually used is embedded —
a document drawing an embedded heading font and base-14 body text is still not PDF/A-conformant,
because PDF/A forbids relying on *any* reader-supplied face, not most of them. `api.invoicing.
pdfa.conformance_gaps`/`assert_conformance` gained an `unembedded_fonts: frozenset[str]` parameter
so the resulting message names *which* font is still not embedded (`"Helvetica (base-14)"`,
`"Helvetica-Bold (base-14)"`, or a custom resource name) — a D5 improvement over "some font is not
embedded," and the specific case a mixed embedded/base-14 document needs named to be actionable.

**With no font configured (every deployment today), this is bit-for-bit the same refusal as
before** — `assert_conformance` still raises naming clause 6.3.4 for any `Conformance.PDF_A_3A`/
`PDF_A_3B` request. Nothing about today's behaviour changed; what changed is that the check is now
honest rather than hardcoded, and it will start succeeding the day real assets exist, with no
further code change.

### 4. Colour: `Color`, `Rect`, and `rg`/`RG` — `pdf.py` had none before this

`api.invoicing.pdf` had no colour operator at all (confirmed by reading `_content_stream` before
this change: every mark drew in PDF's implicit black). `Color` (an RGB triple, `Color.from_hex`
for `#rrggbb`) and an optional `color` field on `Text`/`Line`, plus a new `Rect`/`Page.fill_rect`
for a filled band, are additive: a run/rule/document with no colour requested draws in exactly the
same black as before (`tests/invoicing/test_pdf.py::
test_uncolored_text_is_byte_identical_to_before_color_existed`). A coloured text run is bracketed
in `q`/`Q` so the fill colour it sets does not leak into the ordinary-black run drawn after it —
`rg` sets the *general graphics state*, which otherwise persists across `BT`/`ET` blocks.

`TemplatedPdfRenderer` uses `colors.text` for body/heading fill, `colors.accent` for rules and the
totals row, and `colors.background` for a filled band behind the page header **only when it is not
white** (drawing white-on-white would cost every uncustomised document a rectangle object for
nothing observable).

### 5. FR-TPL-006/007/001 actually drawn, honestly scoped where they are not fully wired

- **Columns**: `template.columns.visible_in_order` decides which of the six `LineColumn`s draw and
  in what order, with the description column fixed first (a document fact, not a toggle — ADR-041's
  own reasoning). Per-line VAT rate is resolved from `view.groups` by `line.vat_treatment` (ADR-037
  already carries no per-line VAT amount, but it does carry a per-treatment rate, which
  `LineColumn.VAT_RATE` asks for). `LineColumn.UNIT` prints an em dash: ADR-041's "no `unit` column
  on `sales_invoice_line`" gap is unchanged by this ADR and is carried forward, named in code
  (`api.invoicing.rendering._column_value`) rather than silently left blank.
- **Content blocks**: all five, with merge tags resolved from the SAME `InvoiceView`/
  `SupplierDetails` every other figure on the page comes from (`_merge_values`) — not a
  preview-only sample table. `legal_identity`'s own locked merge tags (FR-TPL-009's
  `{{supplier_vat_number}}`/`{{supplier_kvk_number}}`) now print art. 226(3)'s and the
  Handelsregisterwet's identifiers, replacing the OLD renderer's hardcoded footer line with the
  template's own text — the same statutory content, now genuinely template-driven.
- **Logo**: drawn (position/size honoured) when `TemplatedPdfRenderer(resolve_logo=...)` is
  supplied a hook returning an already-decoded `api.invoicing.pdf.Image`. **Not wired end to end**:
  ADR-041's "logo upload has no endpoint yet" gap stands unchanged, so `resolve_logo` defaults to
  `None` and no logo is drawn today — see Known gaps.
- **Typography**: `_font_for` uses an embedded font for a `template_font.code` when one is
  configured (see decision #6), else falls back to a base-14 face by `FontWeight`, with a comment
  at the one place that decision is made — never a silent pretence that the chosen typeface was
  used. Type scale, line height and letter spacing are **not yet** applied (see Known gaps).
- **Layout** (`classic`/`modern`/`compact`/`minimal`, FR-TPL-005): **not wired at all**. Every
  `InvoiceTemplate` renders through one geometry regardless of `template.layout`'s value — see
  Known gaps.

### 6. The font binary and the ICC profile: a loading mechanism, never a fabricated asset

`api.invoicing.rendering._load_embedded_fonts` (called once, inside `build_invoice_renderer`) looks
for `<template_font.code>.ttf` files (FR-TPL-002's nine curated codes, `api.templates.model.
FONT_CODES`) at `apps/api/data/invoicing/fonts/` in a checkout (the packaged copy lives alongside
the module, mirroring `api.invoicing.wording._review_file`'s two-location search exactly) — finding
none today, which is every deployment, since **this repository verifiably contains no `.ttf` file
anywhere**. Each found file is subset to `api.invoicing.pdf.winansi_codepoints()` — this platform's
own WinAnsi/Latin-1 text coverage, not one document's text, because the renderer is built once per
process and reused for every invoice it issues; re-parsing and re-subsetting per request for glyphs
that never change buys nothing. A malformed file is skipped (falls back to base-14 for that
typeface), never a startup crash.

The ICC profile (`api.invoicing.pdfa`'s `icc_profile` parameter) is, and remains, **supplied
bytes, never generated** — `pdfa.py`'s own docstring already states why hand-writing one is a bad
idea (a subtly wrong profile validates structurally and renders with shifted colour), and nothing
in this change weakens that. `TemplatedPdfRenderer.render()` does not currently request
`Conformance.PDF_A_3A`/`PDF_A_3B` at all (neither call site asks for an archival document today,
matching production behaviour before this change) — so there is, as yet, no wiring point for an ICC
profile path to plug into. The natural location, when that day comes, is the same `apps/api/data/
invoicing/` directory, e.g. `data/invoicing/icc/srgb.icc`, read once at the same place
`_load_embedded_fonts` is read, and passed through to `render_pdf`'s existing `icc_profile`
parameter — no new mechanism, only a call site that does not exist yet because nothing asks for
PDF/A yet.

**What a deployment needs to supply, and exactly where, to close FR-TPL-015 for real:**

| Asset | Path | Format | Effect once present |
|---|---|---|---|
| A licensed, embeddable font file per curated typeface a deployment wants applied | `apps/api/data/invoicing/fonts/<template_font.code>.ttf` (e.g. `ibm_plex_sans.ttf`) | TrueType-outline `.ttf` (not CFF/`OTTO`) | `_load_embedded_fonts` picks it up on next process start; typography for that code renders with the real face instead of the base-14 fallback |
| An sRGB ICC colour profile | `apps/api/data/invoicing/icc/srgb.icc` (not read yet — see above) | ICC v2/v4 profile bytes | Once a call site passes it to `render_pdf`'s `icc_profile`, `conformance_gaps`' 6.2.2 requirement closes |

Both are additive, no-code-change-required data drops, the same contract `data/invoicing/
wording-review.json` and `data/vat/nl-vat-rules.json` already hold.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Use a PDF/font library (fontTools, pypdf, WeasyPrint, …) for subsetting and embedding | The stated ethos of `api.invoicing.pdf` from before this change: every option brings a rendering engine, multi-script subsetting, and (for most) an image pipeline this platform never uses. A TrueType parser reading exactly `glyf`/`loca`/`cmap`/`hmtx`/`head`/`hhea`/`OS/2`/`post` is a bounded, auditable surface; a general-purpose font toolkit is not. |
| Subset per-document, to exactly the text each invoice draws | Correct in principle (and this is what `api.invoicing.truetype.build_embedded_font` genuinely supports and `test_truetype.py` proves), but `build_invoice_renderer` constructs a renderer once per process, reused for every invoice — re-parsing and re-subsetting a multi-hundred-KB font on every request for a glyph set that never changes is pure cost with no benefit over subsetting once to the platform's own WinAnsi coverage. |
| Fabricate a plausible ICC profile or embed a system font found on the host | Explicitly ruled out by the task and by `pdfa.py`'s own pre-existing docstring: a wrong ICC profile validates and renders wrong, silently; a host-found font is not the deployment's to embed and disappears the moment the process moves to different infrastructure. Refusing honestly is the same posture `api.customers.peppol` takes about inventing a participant id. |
| Flip `fonts_embedded` to `True` unconditionally, or lower the PDF/A bar | Forbidden outright by the brief and by `pdfa.py`'s central decision ("the file may not lie about itself"). The whole point of this ADR is that the gate stays exactly as strict, now backed by a genuine computation instead of a hardcoded constant. |
| Keep two renderer classes, just make the preview one delegate to `MinimalPdfRenderer` internally | Still two classes, two things to keep in step, and still not what either call site actually calls — FR-TPL-008 asks for one engine, not two engines that happen to produce the same output via delegation. |
| Wire `template.layout`'s four variants into distinct geometry now | Out of proportion to this pass: FR-TPL-006/007/004's mechanisms (columns, blocks, colour) were the named gaps from ADR-041's Known gaps list; layout variation is a separate, larger design surface (header arrangement, block placement, totals position per FR-TPL-005) not scoped here. Named as a Known gap below rather than half-built. |
| Build the `template_asset`-to-`Image` fetch pipeline as part of this change | ADR-041 already scoped logo upload out ("no endpoint yet"); building the fetch half without the upload half would let a template reference an asset id nothing can ever populate. `resolve_logo`'s hook shape is what a follow-up wires without touching `TemplatedPdfRenderer` again. |

## Consequences

**Easier.** Exactly one place (`api.invoicing.rendering`) owns invoice drawing; a future change to
how a column, block or colour renders touches one function, visible to both preview and issue by
construction rather than by discipline. `api.invoicing.truetype` is a self-contained, independently
tested unit — closing FR-TPL-015 for real is now "supply two files," not "write a font subsetting
engine."

**Harder.** `api.invoicing.rendering` now imports `api.templates.model` (for `InvoiceTemplate`/
`DocumentType`/etc.), and `api.invoicing.posting`/`posting_repository` now import `api.templates.
model` too — a new, deliberate coupling from the invoicing package to the template domain shape
(never to `api.templates`' repository, service or routes, which keeps the dependency one-directional
and avoids the import cycle a repository-level dependency would risk). A reviewer tracing "what does
invoicing depend on" now has one more package to know about.

**Known gaps, each with a trigger.**

- **`template.layout`'s four variants (classic/modern/compact/minimal) are not wired.** Every
  template renders through one geometry today, regardless of the chosen layout. Trigger: a
  follow-up scoped to FR-TPL-005's per-layout header/block/totals arrangement, likely large enough
  to warrant its own ADR.
- **Type scale, line height and letter spacing (FR-TPL-003) are not applied.** Only the font
  FACE (heading/body/figures, FontWeight) is honoured; the four size/spacing controls are read from
  `Typography` but not yet drawn differently. Trigger: extending `_lay_out`'s fixed point sizes and
  line-height constant into a lookup keyed by these enums.
- **Logo fetch-by-asset-id is not wired end to end.** `TemplatedPdfRenderer(resolve_logo=...)` is a
  real, tested hook (`tests/invoicing/test_rendering.py::
  test_a_logo_carries_the_business_name_as_alt_text`), but nothing calls it in production — ADR-041's
  "no upload endpoint" gap is unchanged. Trigger: the upload endpoint plus a `template_asset` →
  `Image` resolver, wired into `build_invoice_renderer`'s and `get_template_service`'s construction.
- **No licensed font ships, so every document still falls back to base-14 today.** This is
  correct, not a bug — see the hard constraint this ADR was given. Trigger: a deployment placing
  `.ttf` files at `apps/api/data/invoicing/fonts/`, per decision #6's table. No code change required.
- **No ICC profile path is wired, and no call site requests `Conformance.PDF_A_3A`/`PDF_A_3B`.**
  `render_pdf` still defaults to `Conformance.NONE` at both call sites; nothing today claims PDF/A.
  Trigger: a deliberate future decision to start claiming archival conformance for issued invoices,
  which needs (a) an ICC profile at the documented path, (b) at least one `template_font` with a
  supplied `.ttf` actually assigned to every text role a document draws with (mixing an embedded
  heading font with base-14 body text still fails 6.3.4, correctly), and (c) `TemplatedPdfRenderer`
  passing `conformance=Conformance.PDF_A_3A` and the loaded ICC bytes through to `render_pdf`.
- **The byte-identity test proves the mechanism, not "a live preview equals what gets sent five
  minutes later" against REAL data.** `tests/templates/test_render_parity.py` renders the SAME
  (fabricated, deterministic) `InvoiceView` through both real call paths and asserts identical
  bytes — which is the correct scope for a test (real invoice data cannot exist at preview time by
  definition; FR-TPL-008 is a property of the ENGINE, not of coincidental data). The reason this
  cannot be tightened further is structural, not a shortcut: preview never has a real invoice
  number, real customer, or real amounts to render, because none exists yet. This is stated here
  explicitly because the task that produced this ADR asked for the reason to be named if it could
  not be closed, and required that the reason not be "it's a different renderer" — it is not; the
  renderer and every non-data input (template, supplier, locale, document type) are identical on
  both sides, proven by the same test.
- **`api.templates.rendering`'s test coverage moved, not survived.** The old module's own tests
  (over `MinimalTemplatePreviewRenderer`) no longer exist because the class does not; their
  intent — merge tags resolve, columns show/hide/reorder, blocks render — is now covered by
  `tests/invoicing/test_rendering.py` and `tests/templates/test_render_parity.py` against the one
  real renderer instead.
- **Database-backed tests remain unexercised in this environment (no Postgres) —** unchanged from
  ADR-041's own note, carried forward for the same reason: `default_invoice_template`'s SQL and the
  `invoice_template`/`invoice_template_column`/`invoice_template_block` reads it depends on are not
  verified against a real Postgres here.
