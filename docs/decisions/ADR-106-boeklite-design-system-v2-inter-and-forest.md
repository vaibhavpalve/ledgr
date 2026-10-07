# ADR-106: Boeklite design system v2 — Inter, forest and leaf, no serif

- **Status**: Accepted
- **Date**: 2026-10-08
- **Supersedes**: the look adopted in [ADR-104](ADR-104-boeklite-design-system-replaces-the-adr-080-handoff.md) (its `.bk` scoping and bridge stand)

## Context

`design/boeklite-design/` was replaced by a second version of the design system. Its brief
(`CLAUDE_CODE_BRIEF.md`) asks for the earlier kit to be removed outright: Instrument Serif, Geist
and Geist Mono, the seal, the torn receipt, the double underlines and the mono labels. In their
place: one family (Inter, display cut at 600 with negative tracking, tabular figures everywhere),
a deep `forest` brand with light green (`leaf`, `leaf-tint`, `leaf-ink`) meaning "done" and a
single touch of `lemon` meaning "look here", 10px-radius buttons instead of pills, cards with a
1px `line` border instead of a shadow, and totals on a 1px `ink` rule. Behaviour, routes, auth,
i18n (EN/NL) and the theme toggle are to stay as they are: presentation only.

Several token names changed meaning, not only value. `leaf` was a mid green used for link
underlines and the logo; it is now the light "done" green. `cream`, `sand`, `mint` and
`pistachio` are gone (`canvas`, `sunken`, `leaf-tint`, `leaf`).

## Decision

- **Tokens** (`packages/design-tokens/bk-tokens.css`) carry the new palette under the design
  system's own names. The ADR-104 split stays: non-colliding names are global, the names that
  collide with `ui-tokens.css` (`--surface`, `--ink`, `--ink-muted`, `--focus`, `--radius-*`,
  `--font-*`) are declared under `.bk`. The bridge from the ADR-080 and `--ledgr-*` names is
  re-pointed at the new palette; `--font-serif` and `--ledgr-font-mono` resolve to Inter, so no
  screen can fall back to a second family.
- **Old names are renamed at their call sites, not aliased.** An alias would leave `--leaf`
  meaning two things. Every use of `cream`, `sand`, `mint`, `pistachio` and `positive` in
  `apps/web/src` was moved to its new name and reviewed in context; none remains.
- **Inter is self-hosted** (`bk-fonts.css`, `public/fonts/Inter-opsz-400-700-*.woff2`, latin and
  latin-ext, weight and optical-size axes). The brief's Google Fonts link is not used: CSP is
  `font-src 'self'` (ADR-061) and no third-party request leaves the page for typography (ADR-055).
  The Geist and Instrument Serif files are deleted.
- **Rebuilt against the reference renders**: sign-in (`SignInScreen-light.png`), the app shell and
  the overview (`DashboardScreen-light.png`). Every other screen takes the look through the
  bridge (`ui/bk.css`), which now draws sans 13px labels, `sunken` table header bands with 44px
  rows, `leaf-tint` done chips, outlined `negative` error chips and the 1px total rule.
- **One primary button per view** is enforced in the bridge, not only in markup: the legacy
  "every submit button is filled" rule no longer applies to a `ui-btn`, so sign-in's Sign in is
  secondary beside the passkey button.
- **The bottom bar gets its central Capture button** (SCREENS.md; listed as not done in ADR-104):
  Home, Sales, Capture, Grootboek, BTW. Capture is the purchases screen, where uploading is.
- **The marketing page wears `.bk`** too, so it no longer renders in the ADR-080 brown.
- **Brand literals** follow: `theme-color` and the manifest are `canvas` (#fafaf5 / #0f1712),
  the favicon is `boeklite-mark.svg`, and the PNG icons were re-rendered from it.

Copy changed with the design and stays in the catalogue in both languages: "Welcome back" (no
full stop), "or sign in with email", "Continue with Google", "New to Boeklite? Create an
account", the shorter proof points, and the demo card's "BTW 21% detected" / "Balanced to the
cent" / "Booked automatically". The seal and receipt strings are removed.

## Alternatives considered

| Option                                                                                        | Rejected because                                                                                                                                                                                                 |
| --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alias the old token names to the new ones                                                     | `--leaf` would mean the old mid green in some files and the new light green in others; renaming forces each use to be looked at.                                                                                 |
| Load Inter from Google Fonts as the brief says                                                | Breaks CSP `font-src 'self'` and reintroduces the third-party request ADR-055/061 removed.                                                                                                                       |
| Remove the shell's header bar so search and the action sit on the title row, as in the render | The header carries the client's colour rule (FR-FRM-000a), the fiscal-year selector and the user menu on every screen; moving them is a behaviour change, not a restyle. The header is kept, quiet, on `canvas`. |
| Replace the dashboard's cash chart with the render's profit chart                             | The API has no profit series; inventing one breaks ADR-080's "real data only". The cash history draws as the cash card's sparkline and as the chart card.                                                        |

## Consequences

- A screen that still names an ADR-080 or `--ledgr-*` token gets the new look for free, and
  still has to drop those names when it is rebuilt; the bridge is unchanged in shape.
- Sign-in's illustration is a fixed light "product card" on the forest panel in both themes, as
  in the render; its colours are literals for that reason.
- The golden-path e2e spec had drifted from the purchase form before this change (payment method
  removed in ADR-096, the separate Save removed later); its purchase step was updated, and the
  submit step still needs bringing up to date with the current review flow.
