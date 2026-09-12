# ADR-043: Type scale, line height and letter spacing, actually drawn — and confirming no font upload

- **Status**: Accepted
- **Date**: 2026-09-11
- **Implements**: FR-TPL-002 (typography, licensing verified), FR-TPL-003 (typography, mechanism
  completed), FR-TPL-020 (confirmed respected, not weakened)
- **Serves**: FR-TPL-016 (9pt minimum effective body size — the floor every new step still
  respects), CLAUDE.md non-negotiable #4 (adapters, not special cases — the renderer stays the one
  place that decides what a template's typography draws as)
- **Corrects**: ADR-042's own "Known gaps" entry — "Type scale, line height and letter spacing
  (FR-TPL-003) are not applied" — which is the trigger for this ADR
- **Constrained by**: ADR-041 (the `Typography` model this reads from — unchanged, its four enums
  already existed), ADR-042 (the one renderer, `TemplatedPdfRenderer`, and the `pdf.py` writer this
  extends; the FR-TPL-008 byte-identity guarantee that renderer carries)
- **Related**: [ADR-041](ADR-041-invoice-template-model.md) — `Typography`'s four enums, defined but
  not yet drawn differently; [ADR-042](ADR-042-invoice-pdf-rendering.md) — the one renderer and the
  `Color` precedent this ADR's `Tc` addition mirrors

## Context

> **FR-TPL-002.** Typography: a curated set of at least 8 licensed, PDF-embeddable typefaces
> covering serif, sans and monospace, chosen for numeric legibility (tabular figures) and full
> Latin-1 coverage. Separate selection for headings, body and figures.

> **FR-TPL-003.** Type controls: size scale, weight, line height and letter spacing, exposed as a
> small number of sensible steps rather than free numeric entry, so a user cannot produce an
> unreadable invoice.

> **FR-TPL-020.** Custom fonts uploaded by the user are **not** supported in P0; the licensing and
> embedding exposure is not worth carrying at this stage. Revisit at P3.

ADR-041 built `Typography`'s four FR-TPL-003 enums (`TypeScale`, `FontWeight`, `LineHeight`,
`LetterSpacing`) and 0042 CHECK-constrained all four to their closed sets — the API boundary
already made free numeric entry impossible before this ADR touched anything. ADR-042 then built the
one renderer, `TemplatedPdfRenderer`, and applied `FontWeight` and the three font-code selections
(heading/body/figures) to what FACE gets drawn — but named explicitly, as a known gap, that type
scale, line height and letter spacing were read from `template.typography` and never used: every
template drew at the same fixed point sizes and line advance regardless of what a user chose. This
ADR closes that gap: the same three enums now change what size the text draws at, how far a line
advances, and (new primitive) how characters are spaced.

Separately, this task asked for the FR-TPL-002 font catalogue's SIL Open Font License claim to be
verified against the real license files rather than carried forward as an unverified assertion, and
for FR-TPL-020 (no font upload, ever) to be reconfirmed rather than quietly eroded while touching
the same area of the codebase.

## Decision

### 1. Font licensing: verified this session, against the actual license files

All nine `template_font` rows (0042) were checked by fetching each project's real license file, not
by re-asserting the "SIL OFL" label from memory:

| Font | Category | License file fetched |
|---|---|---|
| Inter | sans | `github.com/rsms/inter/LICENSE.txt` |
| Source Sans 3 | sans | `github.com/adobe-fonts/source-sans/LICENSE.md` |
| IBM Plex Sans | sans | `github.com/IBM/plex/LICENSE.txt` |
| Public Sans | sans | `github.com/uswds/public-sans/LICENSE.md` |
| Source Serif 4 | serif | `github.com/adobe-fonts/source-serif/LICENSE.md` |
| IBM Plex Serif | serif | `github.com/IBM/plex/LICENSE.txt` (one file, all three IBM Plex faces) |
| PT Serif | serif | `github.com/google/fonts/ofl/ptserif/OFL.txt` |
| IBM Plex Mono | mono | `github.com/IBM/plex/LICENSE.txt` |
| Source Code Pro | mono | `github.com/adobe-fonts/source-code-pro/LICENSE.md` |

All nine are SIL Open Font License 1.1, and all nine carry the identical operative clause: *"Original
or Modified Versions of the Font Software may be bundled, redistributed and/or sold with any
software, provided that each copy contains the above copyright notice and this license."* That is
what makes embedding one of these families into a customer's generated invoice PDF (once a
deployment supplies a real `.ttf` — still not done anywhere in this repository, per ADR-042's hard
constraint) a licensed act rather than a hopeful one.

**What was NOT re-verified**: tabular (lining) figures and full Latin-1 coverage. No `.ttf` binary
exists anywhere in this repository to open and inspect a glyph table or an OpenType feature list
(`GTAB`/`tnum`), so these claims rest on the well-documented, established engineering of these
specific families — IBM Plex and Adobe's Source superfamily were both explicitly built with full
figure-set support, and IBM Plex Mono/Source Code Pro are tabular by construction as monospace
fonts — rather than on a measurement this session actually performed. 0042's comment block now
states this distinction explicitly rather than presenting both claims at the same confidence level.

### 2. Type scale and line height: explicit lookup tables, never a formula

`api.invoicing.rendering` gains three tables keyed by `TypeScale` (body point size, "small" point
size — the size most of an invoice's body text already draws at — and heading point size) and one
keyed by `LineHeight` (a multiplier on the existing `_LINE_HEIGHT` constant):

| `TypeScale` | body (was `_BODY`, 9.5) | small (was `_SMALL`, 9.0) | heading (was `_HEADING`, 18.0) |
|---|---|---|---|
| `small` | 9.0 | 9.0 | 14.0 |
| `medium` (default) | 9.5 | 9.0 | 18.0 |
| `large` | 10.5 | 10.0 | 22.0 |
| `extra_large` | 12.0 | 11.0 | 26.0 |

| `LineHeight` | multiplier on `_LINE_HEIGHT` (13.0pt) | effective |
|---|---|---|
| `tight` | 0.85 | 11.05pt |
| `normal` (default) | 1.0 | 13.0pt |
| `relaxed` | 1.25 | 16.25pt |

Tables, not a formula, on purpose: a formula that took a scale factor and multiplied every size by
it could in principle be pushed below FR-TPL-016's 9pt floor by a future step nobody re-checked
against that constraint. A fixed table has no such failure mode — `small`'s two body-role columns
already equal 9.0, the floor itself, with zero headroom to go lower, and that is visible by reading
the table rather than by proving a formula's minimum. `Text.__post_init__` (unchanged) still refuses
anything under 9.0pt outright, so this is belt-and-braces: the table cannot produce a violation, and
the primitive would refuse one if it somehow did.

`medium`/`normal` — every `Typography` field's own default, and what `default_template()` and every
template created before this change already carries — resolve to EXACTLY the pre-existing
`_BODY`/`_SMALL`/`_HEADING`/`_LINE_HEIGHT` constants. This is what keeps an unconfigured template's
appearance unchanged: `tests/invoicing/test_rendering.py::
test_medium_type_scale_matches_the_pre_existing_constants` asserts the resulting sizes directly.

`api.invoicing.rendering._lay_out` reads `template.typography.type_scale`/`.line_height` exactly
once, into local variables (`body_size`/`small_size`/`heading_size`/`line_height`), and every one of
its dozens of `page.at(...)` calls uses those instead of the old bare module constants. The three
size roles are unchanged in what they're used FOR — the supplier/customer name and totals row still
draw at "body" size, the bulk of labels/addresses/line items still draw at "small" size, the one
document title still draws at "heading" size — only the actual point value each role resolves to
now varies with the template's chosen step.

### 3. Letter spacing: a new, additive `Tc` primitive on `Text` — mirroring `Color` exactly

`api.invoicing.pdf` had no way to express PDF's `Tc` (character spacing) operator before this
change (confirmed by reading `Text`/`_content_stream` — the same check ADR-042 did before adding
`Color`). `Text` gains `letter_spacing: float = 0.0`, and `Page` gains
`default_letter_spacing: float = 0.0` plus a `letter_spacing: float | None = None` parameter on
`at()` (`None` — the default — means "inherit the page's own default", not "zero regardless"). This
is the SAME additive, opt-in pattern `Color` used: a run/page that never asks for letter spacing
produces byte-identical output to before this field existed
(`tests/invoicing/test_pdf.py::test_no_letter_spacing_text_is_byte_identical_to_before_letter_spacing_existed`).

`_content_stream` now builds a combined graphics-state prefix — `rg` for colour, `Tc` for letter
spacing, whichever are actually set — and brackets it in one `q`/`Q` pair per run, exactly the
mechanism that already stopped a coloured heading's fill colour leaking into the next paragraph.
`Tc`, like `rg`, sets GENERAL graphics state that would otherwise persist across every `BT`/`ET`
block that follows, so the same bracket that protected `Color` protects this too
(`tests/invoicing/test_pdf.py::test_a_letter_spaced_run_does_not_leak_into_the_next_one`).

`api.invoicing.rendering` sets `Page.default_letter_spacing` ONCE, at every `Page()` this function
constructs (there are several — one per page break), from a single template-wide value:

| `LetterSpacing` | `Tc` (points) |
|---|---|
| `tight` | −0.2 |
| `normal` (default) | 0.0 |
| `wide` | 0.5 |

Deliberately small numbers. FR-TPL-003's whole point is that a curated step cannot produce an
unreadable invoice, and a large negative `Tc` collides glyphs into each other while a large positive
one reads as display-type tracking, neither of which belongs on a line-item table. `normal` is
exactly `0.0`, which is what makes the "nothing chosen" case emit no `Tc` operator at all rather
than an explicit-but-zero one — consistent with `Color`'s own "no operator when unset" posture.

Setting the default on `Page` rather than passing `letter_spacing=` to every individual
`page.at(...)` call was a deliberate implementation choice: `_lay_out` has on the order of thirty
such calls, and threading one more repeated keyword argument through every one of them would have
been a much larger, much easier to get wrong diff for the same observable behaviour. `Text.
letter_spacing` — the field FR-TPL-003 actually needs to exist — is unaffected either way; `Page` is
simply the one place that decides what a caller's un-set default resolves to.

### 4. Composing with existing controls: `FontWeight`, and not overrunning the page at the largest step

`FontWeight`'s face selection (already applied by ADR-042's `_font_for`) is untouched by this
change and composes automatically: the SAME `heading_font`/`body_font` resource names are drawn,
now at whatever size `type_scale` picked. The one geometry risk named in this task's own brief — a
`large`/`extra_large` + `wide` + `bold` heading overrunning the right margin — was checked rather
than assumed: the document title is right-aligned by `x = _RIGHT - text_width(title, heading_size)`,
so `text_width`'s own measurement (at whatever `heading_size` is actually used) is what keeps the
title's right edge pinned to `_RIGHT` regardless of point size. `text_width` does not model bold
width or `Tc`'s extra per-character spacing (neither did it before this change — see its own
docstring's "approximate is stated rather than hidden"), so the widest combination (`extra_large` +
`wide`) can overrun the right-aligned edge by a few points — bounded by `Tc`'s own small magnitude
(0.5pt × a title's character count) against `_MARGIN`'s 56pt of headroom past `_RIGHT`. This is a
pre-existing measurement approximation, not a new correctness gap this ADR introduces, and is not
worth a dynamic reflow engine for the reason the task brief itself gave: "you don't need to build a
fully dynamic reflow engine, just make sure nothing visibly breaks at the largest scale step."

### 5. FR-TPL-020: reconfirmed, not touched

No endpoint, route, request body field, or frontend affordance for uploading a font was added
anywhere in this pass. Checked directly: `api.templates.routes.py`'s five registered routes
(list/create/get/update/preview) are unchanged in count and shape; `TypographyBody` still accepts
only the same seven string fields it already did, validated against the fixed `FONT_CODES`
catalogue and the four closed enums — never a binary upload field. `apps/web/src/templates/
TemplateDesigner.tsx`'s new typography section is seven `<select>` elements with no `<input>` of any
kind; `TemplateDesigner.test.tsx` asserts directly that no `input[type="file"]` exists anywhere in
the component and that the typography section carries no `input[type="number"]` or free-text
control at all. FR-TPL-020 is a hard exclusion the task named explicitly, not an oversight to fix,
and nothing in this pass moves toward it.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A formula (e.g. `_BODY * scale_factor[type_scale]`) instead of explicit tables | Could in principle be pushed under FR-TPL-016's 9pt floor by a future step nobody re-derived the minimum for. An explicit table makes "is `small` at or above 9pt" answerable by reading four numbers, not by re-proving a formula's range every time a step is added or changed. |
| A per-call `letter_spacing=` keyword threaded through every one of `_lay_out`'s ~30 `page.at(...)` calls | Correct, and initially considered, but a much larger and more error-prone diff for identical observable output, since every call in one document shares the same template-wide value. `Page.default_letter_spacing` gets the same result from one line per `Page()` construction. |
| Model letter spacing as extra width in `text_width`/`wrap`, so wrapping and right-alignment account for it exactly | Out of proportion to what FR-TPL-003 asks for: the curated `Tc` values are small by design (±0.5pt), and `text_width` was already an approximation before this change (per its own docstring, it does not model bold width either). Reflowing wrap/alignment math around a sub-point adjustment would add real complexity for a visually negligible correction, and the brief explicitly excused this: "you don't need to build a fully dynamic reflow engine." |
| Leave `medium`/`normal`/`normal` mapped through the SAME code path as the other steps, with no explicit "reproduces the old constant" check | Rejected because it would make regression on the byte-identity guarantee silent. The table's `medium`/`normal` rows are written to literally equal the pre-existing constants, and a dedicated test (`test_medium_type_scale_matches_the_pre_existing_constants`) pins that fact rather than trusting it to fall out of the table by coincidence. |
| Re-derive tabular-figures/Latin-1 coverage from an actual glyph table this session | Not possible honestly: no `.ttf` binary exists anywhere in this repository (ADR-042's own hard constraint, unchanged). Claiming a re-verification that could not have happened would be worse than stating plainly what WAS checked (the license files) and what rests on established family characteristics instead. |
| Add a font-upload endpoint or UI control while already touching this area | FR-TPL-020 forbids it outright, by name, as a hard exclusion rather than an oversight — the task brief was explicit that this is "not an oversight to fix." Confirmed no such affordance exists anywhere (see decision #5) rather than silently letting one appear. |

## Consequences

**Easier.** A template's `type_scale`/`line_height`/`letter_spacing` choices now visibly change the
rendered PDF, closing the last of ADR-042's three named typography gaps (font face, which was
already done, being the first). `docs/decisions/ADR-042-invoice-pdf-rendering.md`'s "Known gaps"
entry for this is now resolved rather than open. The `Tc` primitive on `Text`/`Page` is available to
any future caller of `api.invoicing.pdf` without needing to re-derive the bracketing-and-leak
argument `Color` already established — the same pattern is now proven twice, which is what makes it
a pattern rather than a one-off.

**Harder.** `_lay_out` now carries five local variables (`body_size`/`small_size`/`heading_size`/
`line_height`/`letter_spacing`) computed once near its top and threaded through the rest of the
function via ordinary Python locals rather than always reading the same three module constants — a
reviewer scanning for "what size does this draw at" has one more level of indirection to follow
(the lookup tables) than a bare constant offered before. `Page` now carries a piece of
render-time-configured state (`default_letter_spacing`) that a caller must remember to set
consistently across every `Page()` it constructs for one document, rather than every `Text` being
fully self-describing — `api.invoicing.rendering` does this correctly (all seven `Page(...)`
construction sites pass the same value), but a future renderer built on `api.invoicing.pdf` that
forgot to would silently draw with no letter spacing rather than erroring.

**Known gaps.**

- **`figures_font` still does not reach a distinct font resource.** `_template_font_codes` includes
  it (so an embedded font for it is loaded when one exists), but `_lay_out`'s numeric-column values
  still draw with `body_font`, exactly as before this ADR. This is a pre-existing gap this ADR did
  not touch — FR-TPL-002's "separate selection for figures" is not among the three enums FR-TPL-003
  asks this pass to apply, and wiring a genuinely separate figures typeface into the line-item table
  and totals is a distinct piece of work. Trigger: a follow-up that threads a `figures_font`-derived
  resource name into `_column_value`'s callers and the totals block, the same way `heading_font`/
  `body_font` already reach `_lay_out`.
- **Page numbers (`_paginate`) do not scale with `type_scale`.** They still draw at the module-level
  `_SMALL` constant regardless of the template's chosen step, because `_paginate` receives only
  `pages`/`language`, not `template`. Deliberately left alone: page numbers are boilerplate chrome
  (an `is_artifact=True` mark, per FR-TPL-010), not a role FR-TPL-003 was asked to make configurable,
  and threading `small_size` into `_paginate` for a cosmetic-only mark was judged not worth widening
  that function's signature for this pass. Trigger: bundled into whatever follow-up next touches
  `_paginate`'s signature for another reason.
- **`Tc`'s effect on measured text width is not modelled.** `text_width`/`wrap` do not account for
  letter spacing (or bold weight, unchanged from before this ADR), so the widest combination
  (`extra_large` + `wide`) can overrun a right-aligned measurement by a few points — see decision #4.
  Bounded and cosmetic, not a correctness defect a customer would notice on a real invoice's actual
  content. Trigger: only if a future report of visible overlap at the largest steps actually
  surfaces; not scoped here per the task's own "no dynamic reflow engine" instruction.
- **No `.ttf` binary ships in this repository, so every rendered document still falls back to
  base-14 today** (unchanged from ADR-042 — this ADR changes what SIZE/spacing base-14 text draws
  at, not whether a real typeface is embedded). Trigger: unchanged from ADR-042 — a deployment
  placing licensed `.ttf` files at `apps/api/data/invoicing/fonts/`.
