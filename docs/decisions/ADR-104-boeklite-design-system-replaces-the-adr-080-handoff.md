# ADR-104: The Boeklite design system replaces the ADR-080 handoff, screen by screen

- **Status**: Superseded in its look by [ADR-106](ADR-106-boeklite-design-system-v2-inter-and-forest.md); the `.bk` scoping and the bridge below still stand
- **Date**: 2026-10-06
- **Supersedes**: [ADR-080](ADR-080-ledgr-ui-handoff-tokens.md) as each screen moves across

## Context

`design/boeklite-design/` is a new design system: pistachio, lemon and forest on cream; Instrument
Serif, Geist and Geist Mono; pill buttons; the double rule, the seal and the paper receipt. Its
brief asks for the sign-in page first, then the app shell and dashboard, presentation only. Seven of
its token names collide with the ADR-080 set (`--surface`, `--ink`, `--ink-muted`, `--focus`,
`--radius-sm`, `--radius-md`, `--font-*`), so loading it globally would restyle every unmigrated
screen at once.

## Decision

- **Token names stay the design system's own** (`bk-tokens.css`). Names that do not collide are
  global. The seven that do are declared under `.bk`, so a screen opts in by wearing the class.
  When the last screen has moved, the `.bk` blocks fold into `:root` and `ui-tokens.css` goes.
- **Primitives are restyled, not duplicated.** `ui-btn`, `ui-input` and `ui-textbutton` take the
  design system's look inside `.pre-auth`; the same rules lift into `ui.css` with the shell.
  `bk-components.css` carries only what has no primitive (typography, double rule, receipt, seal,
  journal entry).
- **Fonts are self-hosted** (`bk-fonts.css`, `public/fonts`, latin and latin-ext). The design
  system's Google Fonts `@import` is dropped: CSP is `font-src 'self'` (ADR-061).
- **The logo is global now.** `Logo` draws the balanced B with `--glyph-a`/`--glyph-b` tokens (forest
  and leaf on light, cream and lemon on dark); `favicon.svg` is the forest-tile mark.
- **Sign-in behaviour is unchanged.** Same handlers, test ids and API calls. The passkey button moves
  to the top as the one primary action (IAM-012); email and password, Sign in and Google sit below.

## The bridge

The app shell wears .bk too, so every screen inside it takes the design system. Screens not yet
rebuilt resolve the ADR-080 and --ledgr-* token names to the new palette inside .bk
(`bk-tokens.css`, last block), and `ui/bk.css` restyles the `ui-*` primitives and the legacy
`.button-*`, `.chip` and segmented-control classes. Rebuilding a screen means dropping its reliance
on those names; when none is left the bridge goes.

The dashboard maps to real data only: cash (forest) with receivables beneath it, the BTW estimate
(lemon, double rule), unbooked receipts (pistachio), the attention list, and the cash chart in
place of the design's profit chart, since the API has no profit series.

## Not yet done

The bottom bar's central lemon Capture button on every screen, and per-screen rebuilds beyond the bridge.
The PNG icons, `theme-color` (cream, light #f8f6ea and dark #101912) and the manifest colours moved to
the new brand on 2026-10-06; `theme.test` pins the two literals to `cream`.
