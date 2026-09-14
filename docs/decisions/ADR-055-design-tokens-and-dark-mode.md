# ADR-055: Design tokens as a package, and a three-state theme

- **Status**: Accepted
- **Date**: 2026-09-14

## Context

A design review of the four artboards in `.design/` against the shipped web app found that the two
shared no values at all. The canvas describes a warm, single-hue neutral ramp with reserved
semantic colour; the app rendered Google's `#0b57d0` on `#dddddd` rules with browser-default
typography, across three stylesheets totalling about 7 KB. `CaptureScreen.css` said so in its own
header: "It is not a design system: no palette, no tokens, no framework."

That gap blocked four separate requirements, not one aesthetic preference:

- **CMP-012 / FR-LOC-004** (WCAG 2.2 AA). Focus styling existed on exactly two elements in the
  whole app — the capture shutter and the tab bar — so table rows, list rows, links and the client
  switcher had no visible focus state at all (SC 2.4.7). Input borders at `#c7c7c7` on white
  measured about 1.6:1, well under SC 1.4.11's 3:1 for the boundary of a control.
- **FR-FRM-000a** (the active client is unmistakable at all times). The requirement names three
  redundant signals — name, initials, colour. Only two were implemented: `MobileShell.css`
  carried a comment stating that no per-colour styling for `.client-marker` existed anywhere in
  the app, on mobile or web, and every client rendered the same grey.
- **MOB-013** (touch targets). The 48px floor was honoured on the capture screen and the tab bar
  and nowhere else, because an unstyled `<button>` renders at roughly 21px in most browsers.
- **NFR-031** adjacent: monetary figures were rendered in proportional digits, so columns of
  amounts did not line up.

None of these are fixable one screen at a time without the fix forking three ways across web,
mobile and the invoice PDF templates.

Separately, the product has no dark theme. Month-end and quarter-end close happen at night, and
every comparable product in this market now ships one.

## Decision

**A `@ledgr/design-tokens` workspace package is the single source of truth for how LEDGR looks**,
following the existing `packages/*` pattern (`i18n`, `offline-queue`, `shared-types`).

It ships two representations of the same facts:

- `tokens.css` — what the browser applies, as `--ledgr-*` custom properties.
- `src/tokens.ts` — the typed mirror, for the few callers that need a colour as a value rather
  than as a style (`<meta name="theme-color">` is the only one today).

`src/tokens.test.ts` parses the stylesheet and asserts the two agree, token for token, in both
themes. This is the same enforcement `shared-types/src/index.test.ts` already applies between its
TypeScript and migration 0018, and it is the only thing that makes the duplication safe.

Colour is layered: `primitives.ts` holds every literal hex in the product, `tokens.ts` maps those
onto semantic tokens ("the colour of a panel"), and components reference only the semantic token.
No file outside `primitives.ts` contains a hex value.

**The theme has three states, not two.** `system` is the default and is a distinct choice from
picking whichever theme the device is currently in: a user on `system` follows their OS when it
flips at sunset, one who chose `light` does not. This shapes the whole implementation:

- `:root` declares the complete light palette. `@media (prefers-color-scheme: dark)
  :root:not([data-theme="light"])` redefines it for system-dark. `:root[data-theme="dark"]`
  redefines it again so an explicit choice beats a light OS. Every token is declared on the bare
  `:root` first — a token defined only inside a media or attribute block is undefined for the
  majority of viewers, who have made no explicit choice.
- `system` **removes** the `data-theme` attribute rather than stamping the resolved value, which
  is what lets the media query keep following the OS live.
- An inline script in `index.html` applies the stored preference before first paint. It is a
  hand-written copy of `readThemePreference` + `applyTheme` (a module import cannot run that
  early), and `apps/web/src/theme/theme.test.tsx` asserts the copy still agrees with the module.

**Client marker colours are theme-independent** — one set, used in both themes. A client's colour
is that client's identity under FR-FRM-000a; an identity that looked different at night would
defeat the signal it exists to carry.

**Contrast is asserted, not claimed.** `src/contrast.test.ts` implements WCAG 2.1 relative
luminance and checks every text token against every ground it can land on, every status colour
against its own wash, every focus ring, and white initials on all ten client markers — in both
themes. Two palette values were changed because this suite failed on them: `border-strong` went
from the canvas's `#C1BDB7` to `#8F8878` in light and `#524B41` to `#756D5F` in dark, because it
draws the boundary of an input and was measuring about 1.6:1.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A stylesheet in `apps/web/src/` | The invoice PDF templates (FR-INV) and any future React Native client need the same values. A stylesheet cannot be imported by a PDF renderer or a native StyleSheet, so the palette would fork the first time a second surface needed it. |
| Tailwind, or another utility framework | The app has no CSS framework as a dependency today and the screens are written as semantic HTML that the ADR-053 accessibility work depends on. Utility classes would mean rewriting every screen's markup to get a palette, and would put the design system in `class` attributes where no test can assert anything about it. |
| Generate `tokens.css` from `tokens.ts` at build time | Adds a build step to a package that otherwise has none, and makes the stylesheet a build artifact that cannot be read or reviewed in a diff. Authoring both and testing the match costs one test file and keeps both files reviewable. |
| A two-state light/dark toggle | Cannot express the default. Everyone would be forced into an explicit choice they did not make, and the "follow the device" behaviour — which is what most users actually want — would be unreachable. |
| Stamping the resolved theme for `system` users | Freezes the page at whatever the OS was at load. A user whose device switches to dark at sunset would keep a light page until they reloaded. |
| Deriving the dark ramp by inverting the light one | Tried, and it fails on the status colours: a mid-lightness hue inverted onto a dark ground loses contrast against the surface. The dark solids are lifted to roughly L 0.78 and the washes dropped to roughly L 0.22 instead, and `contrast.test.ts` is what settles whether any given value is acceptable. |
| Theme-dependent client marker colours | Would make a client's identity change with the time of day, defeating FR-FRM-000a. The ten colours are mid-toned enough to clear 3:1 against both themes' paper, which the test asserts. |

## Consequences

**Easier.** A new screen gets the palette, the type scale, the 48px touch floor, focus styling and
both themes by writing semantic HTML — the element-level rules in `app.css` are wrapped in
`:where()`, so they carry zero specificity and a screen's own stylesheet always wins without
`!important`. Adding a third theme (high contrast) is a fourth token block, not a redesign.

**Harder.** `tokens.css` and `tokens.ts` must be edited together or the suite fails. That is the
intended cost, and the failure message names the token that drifted.

**What this forecloses.** Any future colour decision has to pass `contrast.test.ts`. A value that
fails cannot ship, which will occasionally mean a designer's preferred hex is rejected — as
`border-strong` already was.

**What is deliberately not done here.**

- The **ledger table** and **review stack** from the design canvas have no React components yet;
  the responsive-grid and keyboard-throughput findings against them cannot be implemented until
  they exist.
- The home screen now leads with the prioritised attention list and demotes the three figures
  beneath it, but a genuinely **deadline-led strip** ("BTW due in 9 days, 4 documents unposted")
  is not built: days-until-filing is not on `DashboardView`, and deriving it in the browser would
  put statutory business logic in the client, which this product does not do. It needs an API
  field first.
- **Webfonts are loaded from Google Fonts**, which is a third-party request on a product that is
  otherwise self-contained and is a GDPR consideration for EU users (PRIV). Self-hosting the two
  families is a follow-up; `display=swap` means text is readable in the fallback meanwhile.
- The **wordmark** changed from a bar-chart glyph to a double-ruled column (the bookkeeper's
  notation for a final figure), but no full brand work — logo lockups, an icon set, app icons —
  was done. The PWA icons remain the ADR-046 placeholders.
