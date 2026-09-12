# ADR-045: Layout variants, page setup, and one-click duplicate/reset

- **Status**: Accepted
- **Date**: 2026-09-12
- **Implements**: FR-TPL-005 (layout, header arrangement, totals position), FR-TPL-010 (page size,
  margins, page numbering, repeating headers, carried-forward subtotals), FR-TPL-019 (one-click
  duplicate and reset)
- **Serves**: FR-TPL-008 (live preview, wired end to end for the first time in the designer UI),
  FR-TPL-016 (tagged structure — every new mark this ADR adds is either tagged content or an
  explicit artifact, never neither), CLAUDE.md non-negotiable #4 (page size is a parameter, not a
  provider-specific special case)
- **Constrained by**: ADR-041 (the `InvoiceTemplate` model this extends with four new fields),
  ADR-042 (the one renderer, `TemplatedPdfRenderer`, and the FR-TPL-008 byte-identity guarantee that
  every prior pass has preserved), ADR-043 (the typography lookup-table pattern this ADR's
  page-geometry tables copy directly), ADR-044 (untouched — the SVG sanitizer and asset pipeline)
- **Related**: [ADR-041](ADR-041-invoice-template-model.md), [ADR-042](ADR-042-invoice-pdf-rendering.md),
  [ADR-043](ADR-043-invoice-template-typography.md)

## Context

> **FR-TPL-005.** Layout: at least 4 starting layouts (classic, modern, compact, minimal), each
> adjustable for header arrangement, logo/address block placement, line-item column selection and
> order, totals block position, and footer content.

> **FR-TPL-010.** Page setup: A4 and US Letter, margins, page numbering, and correct multi-page
> behaviour with repeating headers and carried-forward subtotals.

> **FR-TPL-019.** Reset to default, and duplicate an existing template, are both one action. Users
> experiment more when the way back is obvious.

`Layout` already existed (ADR-041, four members) but carried no visual character of its own beyond
its name — every member rendered identically. Column selection/order (FR-TPL-006) and footer
content (the `FOOTER`/`LEGAL_IDENTITY` blocks, FR-TPL-007) were already independently adjustable
from earlier passes; header arrangement and totals position were not. `_lay_out` hardcoded A4's
595×842pt and a 56pt margin as module constants, and multi-page invoices repeated the table header
(already built) but carried no running subtotal across a page break. `api.templates.routes` had no
duplicate or reset endpoint — creating a fresh scaffold was the closest existing approximation to
"reset," and there was no "duplicate" at all. The designer UI (`TemplateDesigner.tsx`) had no live
preview wired up, despite `POST .../preview` (FR-TPL-008's backend half) already existing from an
earlier pass.

## Decision

### 1. Four new closed-set axes on `InvoiceTemplate`, mirrored in 0043

`HeaderArrangement` (`split` default / `stacked` / `centered`), `TotalsPosition` (`right` default /
`left` / `full_width`), `PageSize` (`a4` default / `letter`), `Margins` (`narrow` / `normal` default
/ `wide`) — four new `enum.Enum` types in `api.templates.model`, four new `text not null default
'...' check (...)` columns on `invoice_template` (migration 0043), following 0042's exact style and
comment conventions. Every default reproduces today's pre-existing geometry: `split` is the header
arrangement `_lay_out` already drew, `right` is where the totals block already sat, `a4` is the only
size that ever existed, `normal` is the pre-existing 56pt margin. This is proven, not merely stated:
`tests/invoicing/test_rendering.py::test_every_new_axis_at_its_default_is_byte_identical_to_omitting_it`
renders the same invoice with the four new fields explicit vs. relying on `InvoiceTemplate`'s own
dataclass defaults, and asserts the bytes are identical.

0043 also adds `invoice_template_name_unique (administration_id, name)` — needed for FR-TPL-019's
duplicate to have a real, retryable collision to name-disambiguate against (see decision #4). This
is the one piece of 0043 not directly asked for by FR-TPL-005/010, added because the duplicate
design the task specifies depends on it existing.

### 2. Page size and margins: `Page` gains `page_width`/`page_height`, defaulting to A4

`api.invoicing.pdf.Page` — previously hardcoding `A4_HEIGHT` in its own `at`/`rule`/`fill_rect`/
`place_image` methods — gains `page_width: int = A4_WIDTH`, `page_height: int = A4_HEIGHT` fields.
`render_pdf`'s `/MediaBox` line now reads each page's own `page_width`/`page_height` instead of the
module constants. `LETTER_WIDTH = 612`, `LETTER_HEIGHT = 792` are new module constants alongside the
existing `A4_WIDTH`/`A4_HEIGHT`. Every existing caller that never names `page_width`/`page_height`
gets the identical A4 geometry it always had —
`tests/invoicing/test_pdf.py::test_default_page_size_is_byte_identical_to_before_page_size_existed`
proves it the same way ADR-042/043 proved their own additive parameters.

`api.invoicing.rendering._lay_out` resolves `template.page_size`/`template.margins` into local
variables **named identically to the module's own `_MARGIN`/`_RIGHT`/`_PAGE_BOTTOM` constants** —
Python's ordinary scoping rules mean this assignment shadows the module constant for the rest of
the function (and every closure `_lay_out` defines, since they close over the enclosing scope by
reference). This was the deliberate mechanism for threading page geometry through a ~700-line
function with dozens of existing call sites without rewriting each one: every pre-existing reference
to `_MARGIN`/`_RIGHT`/`_PAGE_BOTTOM` inside `_lay_out` already means "this template's own geometry"
without being individually touched. Only the few call sites OUTSIDE `_lay_out`'s own scope
(`_column_edges`, `_paginate`) needed an explicit new parameter, because Python's shadowing does not
cross a function boundary.

### 3. `Layout`'s own visual character: a small internal lookup, never user-adjustable directly

`_LayoutCharacter(uses_background_band: bool, row_spacing_multiplier: float, draws_rules: bool)`,
one instance per `Layout` member:

| `Layout` | background band | row spacing | rules |
|---|---|---|---|
| `classic` (default) | no | 1.0× | yes |
| `modern` | yes (tinted) | 1.0× | yes |
| `compact` | no | 0.8× | yes |
| `minimal` | no | 1.0× | no |

`classic` is bit-for-bit what `_lay_out` already drew. `modern` draws a filled band (`Page.
fill_rect`, from ADR-043's precedent) behind the whole header, sized to the header's own drawn
height rather than a fixed guess, tinted via a new `_tint(color, toward_white)` — a plain per-channel
linear blend toward white, NOT `ColorScheme.contrast_warnings`'s WCAG relative-luminance formula
(that formula judges a ratio; it does not itself produce a lighter colour, and reusing its maths
here would answer a question this decision does not ask). `compact` multiplies `line_height` by
0.8 — every row-to-row gap in `_lay_out` that derives from `line_height` shrinks with it, which is
what makes it genuinely denser rather than cosmetically relabelled. `minimal` guards both of
`_lay_out`'s `page.rule(...)` calls behind `character.draws_rules`.
`tests/invoicing/test_rendering.py::test_the_four_layouts_render_pairwise_different_bytes` renders
one invoice under all four and asserts every pair's bytes differ.

### 4. Header arrangement and totals position: independent axes, dispatched inside `_lay_out`

`HeaderArrangement` governs ONLY the logo/supplier-address/title/reference/metadata block at the top
of the page — the customer ("bill to") block and everything below it is unaffected and continues to
flow from whatever `row` the header dispatch leaves behind. `split` is byte-identical to
pre-existing output (proven the same way as decision #1); to make that literally true, `split`'s
metadata column is drawn at its ORIGINAL position in the code (after the customer block, before the
INTRO content block) rather than moved earlier alongside the rest of the header — moving it would
have reordered `page.texts`, which reorders both the content stream bytes and the MCIDs
`_build_structure` assigns from them, even though nothing would look different. `stacked` and
`centered` draw everything (including metadata) inline, single-column, since neither has a
byte-identity obligation to any pre-existing geometry.

`TotalsPosition` governs only the final three-line summary (subtotal / VAT total / total) — the
per-rate VAT breakdown immediately above it is unaffected, since FR-TPL-005 names "the totals block"
as one thing and the per-rate breakdown is separate, statutorily-required content (art. 226(8)(10))
with no positioning control of its own. `right` (default) is pre-existing geometry; `left` collapses
label and amount into one left-aligned string at the margin; `full_width` draws the label at the
margin and the amount right-aligned at the right edge (RIGHT's own two-part shape), behind a
`_tint`-coloured band spanning margin to margin.

### 5. Carried-forward subtotals: an artifact-marked pair of lines, bracketing every page break

When the line-item loop's existing `if row + needed > _PAGE_BOTTOM:` branch fires, a running
`Decimal` accumulator (`running_net`, incremented by `line.line_net` after each line is drawn) is
printed twice: **"Subtotal carried forward: {amount}"**, right-aligned, on the page about to be
replaced — BEFORE the existing `footer()` call, so it reads as the last thing on that page — and
**"Balance brought forward: {amount}"**, right-aligned, on the new page, immediately after the
repeated `table_header()` call and before the first resumed line item. Both are marked
`is_artifact=True`, for the identical reason the pre-existing running footer and page numbers
already are: this text sits between the last line-item cell and the next page's repeated header
row, and ordinary paragraph content there would make `_build_structure`'s "any non-cell run closes
the open table" rule end the line-item Table at every page break, splitting one continuous table
into as many pieces as there are pages. New i18n keys `invoice.pdf.carried_forward`/
`invoice.pdf.brought_forward`, both languages, following every other `invoice.pdf.*` key's shape.

A single-page invoice never enters the overflow branch, so it never draws either line — proven by
`test_a_single_page_invoice_never_mentions_carrying_forward`. A synthetic invoice forced across
three-plus pages
(`test_carried_forward_and_brought_forward_agree_across_multiple_breaks`) asserts every
"brought forward" figure equals the immediately preceding "carried forward" figure, the running
total strictly increases across successive breaks, and the document's actual total (`view.net`)
is unaffected — carrying forward changes presentation only, never the figure a customer owes.

### 6. Duplicate: read the source, retry the name, insert once

`POST .../invoice-templates/{id}/duplicate` (no request body — everything needed comes from the
source row). `InvoiceTemplateService.duplicate_template` reads the source template, then calls the
existing `repository.create(...)` with every one of its fields (layout, logo — including
`asset_id`, typography, colours, columns, blocks, and the four new axes), `is_default=False`
(a duplicate never steals FR-TPL-011's one-default-per-administration slot), name `"Copy of
{name}"`. On a name collision (0043's `invoice_template_name_unique`, surfaced as the repository
now distinguishing `TemplateNameConflict` from the pre-existing `TemplateConflict` by inspecting the
failed constraint's name) it retries `"Copy of {name} (2)"`, `"(3)"`, ... up to twenty attempts
before giving up with a clear error — a bound named as exceedingly-rare-in-practice in the code
comment next to it, not expected to ever actually bind in normal use. `_default_columns`/
`_default_blocks` (the compliant scaffold) are duplicated a third time into `api.templates.service`
rather than imported from `api.templates.routes`, because `api.templates.routes` already imports
FROM `api.templates.service` — importing the other direction would be circular. This mirrors the
identical choice `api.invoicing.rendering` already made for its own copy of the same two functions.

### 7. Reset: the same save path, not a second write route

`POST .../invoice-templates/{id}/reset` accepts only `{version}` (`ResetTemplateBody`), exactly
`TemplateUpdateBody`'s own optimistic-concurrency shape. `InvoiceTemplateService.reset_template`
reads the current row (to preserve `name`/`is_default`), builds a `TemplateFields` from the built-in
defaults (`Layout.CLASSIC`, `Logo(asset_id=None)`, the same `Typography`/`ColorScheme` values
`TemplateBody`'s pydantic defaults already express, `_default_columns()`/`_default_blocks()`, and
`HeaderArrangement.SPLIT`/`TotalsPosition.RIGHT`/`PageSize.A4`/`Margins.NORMAL`), and calls the
EXISTING `update_template` — the same compliance gate and optimistic-concurrency check every
ordinary save already goes through, rather than a second write path that could silently drift from
it. A stale `version` raises the identical `StaleTemplateVersion` an ordinary edit-conflict does.

### 8. Frontend: more sections of the same component, a debounced live preview, two buttons

`TemplateDesigner.tsx` gains a fifth `<select>`-driven section (layout, header arrangement, totals
position, page size, margins — options from new `@ledgr/shared-types` exports
`LAYOUTS`/`HEADER_ARRANGEMENTS`/`TOTALS_POSITIONS`/`PAGE_SIZES`/`MARGINS`, following the identical
pattern `TYPE_SCALES` etc. already established), a live preview panel, and Duplicate/Reset buttons —
as more sections of the SAME component, not a new screen, matching every prior pass's structure.

The component's entire current draft (columns, typography, logo, blocks, and the five new fields) is
assembled into one `draftBody` (`useMemo`), which both `write()` (save) and a new debounced
`useEffect` (500ms after the last change to `draftBody`) build their request from — the same value
both ways, so save and preview can never silently observe different drafts. The preview response's
`content_base64` decodes to a `Blob`, rendered via `URL.createObjectURL` in an `<iframe>`; the
object URL is revoked whenever a new one replaces it or the component unmounts, via the identical
two-effect pattern (`Blob` state → derived object-URL state, revoked in that second effect's own
cleanup) the existing logo preview already uses. The preview's `violations` are written into the
SAME `violations` state save-time 422s already populate, so FR-TPL-009's inline per-field rendering
shows a live-preview violation exactly the way it shows a save-time one, with no second rendering
path to keep in step.

Duplicate and Reset are both `type="button"` (never submit the form) and call
`onChanged?.(next)` with the server's answer — this component's job ends at handing back the new
or reset template, consistent with how `onChanged` already works for an ordinary save; a wrapper
decides what "now editing the duplicate" means, the same seam `isNew` already documents for the
bootstrap flow.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Compute page geometry as local variables with NEW names (`margin`, `right`, `page_bottom`) throughout `_lay_out`, updating every one of its dozens of existing references | Correct, but a far larger and more error-prone diff for identical behaviour. Shadowing the module's own `_MARGIN`/`_RIGHT`/`_PAGE_BOTTOM` names is a standard, if unusual-looking, Python idiom, and it means every pre-existing line inside `_lay_out` (and its closures) is already correct without being touched — only the few call sites that cross a function boundary (`_column_edges`, `_paginate`) needed a new parameter. |
| Reuse `ColorScheme.contrast_warnings`' WCAG relative-luminance formula to derive `MODERN`'s tint colour | That formula answers "is this ratio too low," a judgement; it does not itself compute a lighter colour. Building a second colour-math path here to reuse a formula built for a different question would be the wrong kind of thoroughness. A plain per-channel linear blend toward white is simpler, always legible at a high blend fraction, and is what the task brief's own "use your judgement, don't invent new colour math" guidance pointed at. |
| Give `HeaderArrangement`/`TotalsPosition` their own row-by-row byte-identity proof the way the four brand-new axes get one | `SPLIT`/`RIGHT` (their defaults) ARE covered by the same "every new axis at its default" byte-identity test as `PageSize`/`Margins`/`layout` — they are two of the five fields that test sets explicitly and compares against the implicit-default render. A second, narrower test would duplicate that coverage for no added confidence. |
| A `document_type`-shaped "one row per layout variant" table, mirroring how `DocumentType` is a parameter rather than a column (ADR-041) | Not applicable here: `DocumentType` is a parameter because ONE template must apply the same look across six document kinds. `Layout` is the opposite shape — a template picks exactly one layout at a time, adjustable afterward — so a plain enum column (mirroring `layout`'s own pre-existing shape from ADR-041) is the right fit, not a parameter threaded through the renderer's call signature. |
| Duplicate without a database-enforced name-uniqueness constraint, generating a probably-unique name (e.g. appending a timestamp) | Rejected because FR-TPL-019's spec explicitly describes a RETRY loop against a real collision, and a probably-unique name (a timestamp, a random suffix) would make two templates named identically an unenforced possibility elsewhere too, not just for duplicates - e.g. two users creating a template named "Winter 2026" independently. A real uniqueness constraint (0043) makes the retry loop meaningful and closes that gap for every write path, not only this one. |
| A second write route for reset, writing the built-in defaults directly | Rejected per the task's own instruction and this codebase's own layered-gate posture (mirroring 0042's three-layer FR-TPL-009 argument): bypassing `update_template`'s compliance gate would mean a future change to what "default" means could introduce a non-compliant reset target with nothing to catch it. Reusing the exact same save path costs nothing extra today (the defaults are compliant by construction) and removes that whole failure mode for tomorrow. |
| Merge live-preview violations into a SEPARATE state slot from save-time violations, to avoid any cross-talk between the two | Rejected as unnecessary complexity for what the task explicitly asks for ("reuse that function/logic rather than duplicating it") — see this ADR's own Known Gaps entry on the one real (and narrow) consequence of sharing the slot. |

## Consequences

**Easier.** A template's layout, header arrangement, totals position, page size and margins are now
genuinely adjustable and genuinely visible in the rendered PDF and in the designer's own live
preview — the last of FR-TPL-005/010's controls this designer did not yet expose. FR-TPL-019's
"the way back is obvious" is now literally one button each. `_tint` and the `_LayoutCharacter`
lookup are small, reusable primitives available to any future layout-shaped control in this
renderer. The `_MARGIN`/`_RIGHT`/`_PAGE_BOTTOM` shadowing pattern, once understood, makes every
future geometry-dependent addition inside `_lay_out` automatically page-size/margin-aware for free.

**Harder.** `_lay_out` now shadows three module-level names with per-call local variables — a
reader unfamiliar with the pattern who greps for `_MARGIN`'s definition and finds only the
module-level `56.0` constant will be misled about what a given line inside `_lay_out` actually
draws at; a comment at the shadowing assignment names this explicitly, but it remains a real
comprehension cost this ADR accepts in exchange for not rewriting dozens of call sites. The header
arrangement dispatch introduces a genuine special case (`SPLIT`'s metadata column deferred to its
original position, `STACKED`/`CENTERED`'s drawn inline) that a future fourth header arrangement
would need to reason about explicitly rather than following one uniform shape.

**Known gaps.**

- **`MODERN`'s header tint band under `HeaderArrangement.SPLIT` does not cover the metadata
  column.** The band is sized to `row` at the point the header dispatch finishes, and `SPLIT`
  defers its metadata column to a LATER point in the draw order (decision #4's byte-identity
  requirement) — so a `MODERN` + `SPLIT` template's tinted band ends before the invoice-date/
  due-date figures in the top-right print. Cosmetic only (both `MODERN` and `SPLIT`'s deferred
  metadata are correctly positioned individually; only their combination under-sizes the band).
  Trigger: revisit if `MODERN`'s band needs to account for `SPLIT`'s specific geometry, at the cost
  of `SPLIT` no longer being a single, uniform "just use `row`" case for every layout.
- **`TotalsPosition.LEFT` and `FULL_WIDTH` do not reposition the per-rate VAT breakdown line(s)
  immediately above the three-line summary**, only the summary itself. Deliberate (see decision #4)
  — FR-TPL-005 names "the totals block," and the per-rate breakdown is separately statutory,
  unpositioned content — but a user choosing `LEFT` sees the VAT-per-rate line stay right-aligned
  while the summary below it moves, which could read as inconsistent. Trigger: only if user
  feedback asks for the two to move together; not implied by the requirement as written.
- **The live preview's violations and save-time violations share one React state slot.** A preview
  response that resolves after a failed save (both keyed to the debounce timer and the network,
  neither of which this pass adds request-generation/cancellation logic for) could overwrite the
  save's violations with the preview's — in practice these agree, because `api.templates.compliance.
  check()` is a pure function of the draft and both calls check the SAME draft, but a network race
  where an in-flight preview response for an OLDER draft arrives after a newer save attempt is not
  guarded against. Trigger: add a generation counter or `AbortController`-based cancellation to the
  preview effect if this is ever observed in practice; not implemented here since the two only
  disagree under a genuine race, not under ordinary sequential use.
- **Page numbers (`_paginate`) and the carried-forward/brought-forward lines both draw at
  `_SMALL`/`small_size` regardless of layout's `row_spacing_multiplier`** — `COMPACT`'s denser
  spacing tightens gaps BETWEEN lines but does not shrink these particular artifacts' own font size
  (nor did ADR-043's per-layout convention ask it to; `_paginate` already draws at a fixed size
  regardless of `type_scale` too, per ADR-043's own named gap). Consistent with the pre-existing
  precedent, not a new inconsistency this ADR introduces.
- **`PageSize.LETTER` and non-`NORMAL` `Margins` are not exercised against the SVG sanitizer, the
  font/typography wiring, or the compliance gate beyond what this ADR's own tests cover** — those
  three areas were explicitly out of scope ("do not touch") for this pass, and nothing about page
  size or margins interacts with any of them structurally, but no test in this pass specifically
  combines, say, `PageSize.LETTER` with an embedded font. Trigger: only if a future pass touching
  font embedding needs to confirm page-size independence explicitly.
