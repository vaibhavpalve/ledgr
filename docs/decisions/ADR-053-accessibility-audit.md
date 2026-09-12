# ADR-053: WCAG 2.2 AA audit of the web app and PWA, and automated testing in CI

- **Status**: Accepted
- **Date**: 2026-09-13
- **Implements**: CMP-012 (PRD §9), FR-LOC-004 (PRD §20)
- **Related**: [ADR-046](ADR-046-mobile-pwa.md) (the PWA this audit covers), [ADR-047](ADR-047-one-tap-capture.md)
  and [ADR-049](ADR-049-mobile-client-header.md) (MOB-013's touch-target minimums, already in place before this
  audit), [ADR-038](ADR-038-customer-master.md) (the pattern this ADR's ADR-052 sibling shows for the API side)

## Context

> **CMP-012.** Accessibility conformance with WCAG 2.2 Level AA, aligned to EN 301 549 and the European
> Accessibility Act.
>
> **FR-LOC-004.** WCAG 2.2 AA across web and mobile, verified by automated testing in CI and by manual audit
> before GA.

`apps/web` is, today, the mobile-first PWA shell (`App.tsx` → `MobileShell.tsx`'s five tabs) plus one built-but-
not-yet-mounted desktop screen (`TemplateDesigner.tsx`, awaiting the desktop web shell). Neither had ever been
checked against WCAG 2.2 AA, and no automated accessibility testing existed anywhere in the repository —
FR-LOC-004's "verified by automated testing in CI" was entirely unmet.

The app was, on inspection, considerably better built than a from-scratch audit usually finds: `ClientSwitcher`
already implemented the ARIA combobox/listbox pattern correctly (`aria-activedescendant`, full keyboard
reachability), every screen had a correct single-`<h1>`-per-view heading structure, every `<select>` was
correctly `<label>`-wrapped, the one `<iframe>` had a `title`, the one real `<img>` had a translated `alt`, and
`index.html`'s viewport meta did not disable pinch-zoom. The gaps found were real but narrow.

## Decision

### Fixed directly (no design input needed)

1. **`.visually-hidden` had no CSS definition anywhere**, despite being used twice in `TemplateDesigner.tsx`
   (a screen-reader-only label beside the locked-column lock icon, and beside each visibility checkbox). The
   text was rendering fully visible, doubled up beside what it was meant to silently describe. Added
   `apps/web/src/accessibility.css`, imported once from `main.tsx` so every screen gets it.
2. **Two dialogs (`ClientSwitcher`'s `role="dialog"`, `CaptureScreen`'s `QualityPrompt`'s
   `role="alertdialog"`) had no `aria-modal`, never moved focus into themselves, never trapped Tab, and never
   restored focus on close.** `role="alertdialog"` in particular promises an interruption FR-EXP-001's own
   requirement text describes ("glare and blur warning with retake prompt") — without any of the above, that
   promise was not kept for a keyboard or screen-reader user. Added `apps/web/src/useModalFocus.ts`, a small
   hook both dialogs now share (`useModalFocus.test.tsx` tests it directly against a plain two-button dialog);
   added `aria-modal="true"` to both.
3. **No focus management when `MobileShell` switches tabs.** With no router, a tab switch is a full-screen
   replacement exactly like a page navigation, but nothing moved focus the way a real navigation would.
   `MobileShell.tsx`'s content area is now a `<main tabIndex={-1}>`, focused on every tab change after the
   first render.
4. **Inconsistent `:focus-visible` styling.** `.capture__shutter` had one; `.capture__secondary` and the
   mobile tab bar's buttons relied on the browser default alone. Added matching rules using the same brand
   blue already in use, rather than leaving one button in the app the only one with a deliberately-designed
   focus ring.
5. **Two ARIA structure bugs axe-core found that the manual review missed** (see "How this was verified"
   below): `HomeScreen.tsx`'s `<dl>` had a caption `<p>` as a third child of a group alongside its `<dt>`/`<dd>`
   — invalid, since a `<div>` inside `<dl>` may only contain `<dt>`/`<dd>` — fixed by making the caption a
   second `<dd>` for the same term rather than an unrelated paragraph; `ClientSwitcher`'s "no matches" row
   was a bare `<li>` inside `role="listbox"`, which ARIA-requires option children — fixed by giving it
   `role="option"`, `aria-disabled="true"` and `aria-selected="false"`, the shape a disabled option takes.

### Verified, not fixed (already compliant)

Every colour actually in use in `MobileShell.css` and `CaptureScreen.css` was checked against WCAG's 4.5:1
(normal text) / 3:1 (non-text/UI) thresholds by direct calculation from the hex values — `#666666`,
`#a15c00`, `#444444` and `#0b57d0` all sit at 5.19:1–9.73:1 against `#ffffff`, and the shutter's white text on
`#0b57d0` sits at 6.39:1. Disabled-control contrast (`#8a8a8a` on `#f7f7f7`) is correctly exempt under WCAG's
own carve-out for inactive UI components and was left alone. No change was needed anywhere in the current
palette.

### Automated testing added to CI

**`eslint-plugin-jsx-a11y`** (`eslint.config.js`, scoped to `apps/web/**/*.{ts,tsx}` alongside the existing
`react-hooks` plugin), running inside the existing `lint-web` job. Found one real issue on introduction —
flagged, but a legitimate false positive for the ARIA combobox pattern (`ClientSwitcher`'s `<li role="option">`
has `onClick` for mouse convenience; its keyboard path is Enter on the combobox input via
`aria-activedescendant`, which the linter cannot see) — suppressed with a one-line, reasoned
`eslint-disable-next-line` rather than papered over.

**`axe-core`**, called directly (`apps/web/src/testing/axe.ts`) rather than through a Jest-oriented wrapper
(`jest-axe`) this Vitest project has no other reason to depend on. `color-contrast` is explicitly disabled in
the helper: jsdom does no layout or paint, so that check cannot produce a trustworthy answer in this
environment and would either false-fail on everything or (more likely, since axe reports uncertain checks as
`incomplete` rather than `violations`) silently check nothing — asserting only on `violations`, never
`incomplete`, is what keeps the gate honest about what it actually verified, matching the same reasoning
`api.ledger.integrity` (this codebase's backend integrity job) applies to its own "examined nothing" case.
Contrast is what the by-hand calculation above verifies instead.

Tests added directly inside the existing component test files that already had working render harnesses
(`MobileShell.test.tsx` — all five tabs, settled; `ClientSwitcher.test.tsx` — open, with and without results,
plus the new focus-trap/restore behaviour; `CaptureScreen.test.tsx` — base screen and the quality prompt open;
`TemplateDesigner.test.tsx` — a fully-configured template and a 422 rendering a violation inline;
`PreAuthScreen.test.tsx` — both screens, in both shipped languages), rather than one new file rebuilding every
harness from scratch. Every added describe block is named `"CMP-012/FR-LOC-004: no automated WCAG 2.2 AA
violations"`, which is what makes them independently selectable.

**`apps/web/package.json`**'s new `test:a11y` script (`vitest run -t "CMP-012/FR-LOC-004"`) runs exactly those
tests by name, and a new `a11y-web` CI job runs it as its own check — the same suite `test-web` already runs
in full, broken out so a WCAG regression is its own failing status check rather than one lost line inside
"Unit tests", the reason `dependency-scan-web` is already its own job rather than a step in `lint-web`.

### How this was verified

Every fix above and every test file change was run against the actual local toolchain before being called
done: `pnpm --filter @ledgr/web run lint`, `run typecheck`, `run test` (359 passed), and `run test:a11y` (13
passed) all green. The two ARIA structure bugs in "Fixed directly" item 5 were found BY those axe tests during
this work, not by the manual review that preceded them — the concrete demonstration of why FR-LOC-004 asks for
both automated testing and manual audit rather than either alone.

## Needs a design decision (not fixed here)

1. **`.client-marker`'s per-colour backgrounds are entirely unstyled** — a pre-existing gap this audit did not
   create (`MobileShell.css`'s own comment already flags it) but one a colour-contrast requirement now
   attaches to: whenever an actual palette is chosen, each swatch's background needs ≥3:1 contrast against
   its initials text (white or dark, chosen per swatch) and the set as a whole should stay distinguishable
   under common colour-vision deficiencies — not fixable without the palette itself, which is a visual design
   decision. The initials-plus-name redundancy `ClientHeader`/`ClientSwitcher` already build in means colour
   is never the ONLY signal, which lowers the stakes of this one but does not remove the need for it.
2. **`QualityPrompt`'s visual treatment.** This audit fixed its ARIA/focus correctness (`aria-modal`, trap,
   restore) unconditionally, but whether it should also render as a true overlay with a backdrop — matching
   what `role="alertdialog"` visually implies to a sighted user, versus staying inline above the drop zone as
   it does today — is a visual design call this audit did not make.
3. **No shared UI primitives or design-tokens file exists anywhere in `apps/web`.** Every accessibility
   property (focus rings, contrast, touch targets) is currently re-derived, correctly, by hand in each new
   component's own stylesheet — `CaptureScreen.css`'s own header says as much: "not a design system: no
   palette, no tokens, no framework." That has held up so far because the team building it has clearly been
   deliberate every time, but it does not scale past a handful of screens without a real risk that the next
   button is styled by someone who has not read the four files this audit did. Worth a decision, not a
   silent default either way.
4. **`ClientSwitcher` is built, tested, and not mounted anywhere yet** (`App.tsx`/`MobileShell.tsx` do not
   render it — confirmed by grep, not assumed). Its accessibility is now correct in isolation; whatever
   eventually wires it in will need to also give the REST of the page an `inert`/`aria-hidden` treatment while
   it is open, since `useModalFocus`'s Tab-trap only stops keyboard escape — a screen reader's own virtual
   cursor can still browse into sibling content unless the mounting site marks it inert, and this hook cannot
   do that on the mounting component's behalf without knowing what the siblings are.

## Alternatives considered

| Option | Rejected because |
|---|---|
| `jest-axe` instead of calling `axe-core` directly | Pulls in Jest-shaped typings and conventions this Vitest project has no other reason to carry, for a wrapper around the same engine |
| Playwright + `@axe-core/playwright` for real-browser contrast checks | A second test runner and browser-install step, solely to cover the one check (`color-contrast`) this audit already verified by direct calculation against the actual, small, hand-picked palette in use. Worth revisiting if/when the palette grows past what a person can check by hand |
| One new top-level `axe.test.tsx` rebuilding every screen's render setup | Every screen already had a working, fixture-complete test file; duplicating that setup elsewhere is more surface to keep in step for no behavioural gain |
| Fold the axe tests into `test-web` with no separate CI job | Works today, but a WCAG regression would read as an anonymous failure inside "Unit tests" rather than its own named, actionable check |
| Build the `.client-marker` palette and the QualityPrompt backdrop now, to close every finding | Both are visual design decisions (colour choices, whether to add a backdrop), not accessibility mechanics — the same line CLAUDE.md draws elsewhere between what an audit fixes and what it flags for a decision only a designer should make |
| A full shared component library / design-tokens file, built as part of this audit | Far larger than what four gaps in an otherwise well-built app justify; the "needs a design decision" list names this as worth deciding on its own, deliberately, rather than started as a side effect of an accessibility pass |

## Consequences

**Easier.** FR-LOC-004's "automated testing in CI" is now true rather than aspirational, and the pattern
(`useModalFocus`, `src/testing/axe.ts`, the `test:a11y` filter) is there for every screen built after this one
to reuse rather than rediscover.

**Still to do.** The four items above need a design decision, not more engineering time, before they can be
closed. `PIVOT`/manual audit before GA (FR-LOC-004's other half) still needs to happen against a real screen
reader and real assistive technology — nothing here substitutes for that, including the axe suite: jsdom's own
limits (no layout, no real AT) mean it structurally cannot be the whole verification story, only the part that
runs on every push.

**Not built.** `TemplateDesigner.tsx` was audited and given its own axe coverage even though it is not mounted
anywhere yet, on the view that fixing it now is cheaper than re-discovering the same gaps once a desktop shell
exists to put it in — but that shell, and therefore TemplateDesigner's own landmark/heading position within
it, is still someone else's decision to make.
