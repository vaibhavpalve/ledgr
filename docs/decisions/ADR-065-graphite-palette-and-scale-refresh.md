# ADR-065: A cool graphite palette and refreshed type/spacing scale, replacing ADR-055's warm ramp

- **Status**: Accepted
- **Date**: 2026-09-18
- **Amends**: [ADR-055](ADR-055-design-tokens-and-dark-mode.md) — the neutral ramp, the four status
  colours, the ten client marker colours, and four of the scale tokens (`type-small`, `type-body`,
  `type-page`, `type-display`, `space-6`, `space-7`, `radius-control`, `radius-panel`). ADR-055's
  architecture — the token package, the three-state theme, the semantic layer, `contrast.test.ts`,
  `tokens.test.ts` — is unchanged and unamended: this ADR replaces *values*, not the machinery that
  enforces them.

## Context

This was a deliberate request to redesign LEDGR's visual identity, not a response to a defect.
ADR-055's warm OKLCH-80 ramp was itself a considered, tested choice — the request to replace it was
made and confirmed explicitly, understanding that it discards that earlier work rather than
building on it. The reasoning below is why the *replacement* looks the way it does, not an argument
that ADR-055 was wrong.

## Decision

**The neutral ramp moves from a warm hue-80 OKLCH ramp to a nearly-neutral cool "graphite" ramp**
(light: `#FAFAFA` ground / `#FFFFFF` paper / `#18181B` ink; dark: `#09090B` ground / `#18181B` paper
/ `#FAFAFA` ink) — the family most current SaaS dashboards use. Every value was solved against
`contrast.test.ts`, the same discipline ADR-055 used, not picked by eye: `packages/design-tokens/
src/primitives.ts` documents the exact WCAG relative-luminance targets each group was solved for.

**The accent moves from teal-on-warm to a deep teal (`#0F766E` light / `#2DD4BF` dark) on the cool
ramp**, kept distinct in hue from `positive` (a true green) so the two are never mistaken for each
other, and deliberately not indigo/violet — the hue the redesign skill's own audit names as the most
common "generic AI product" fingerprint, and one this product has no reason to invite.

**All ten client marker colours were resolved for a fixed relative-luminance band (~0.17)**, per
hue, rather than a shared HSL lightness — a shared lightness does not give a shared *luminance*
across hues (green and yellow reach a given luminance at a much lower lightness than blue and
violet do), which is why naively reusing ADR-055's "one lightness, sweep the hue" approach failed
`contrast.test.ts` on five of the ten markers on first attempt. Each was solved independently until
it cleared 3:1 against both a white and a near-black paper simultaneously, plus 3:1 white-on-marker.
The ten *names* (`indigo`, `amber`, `teal`, …) and their order are unchanged and cannot change: they
are data, stored in `client_colour` (migration 0018) and returned by the API as strings — only the
hex each name maps to did.

**Four scale tokens changed value, none changed count or role**: `type-small`/`type-body` moved to
the 14/16px convention most current dashboards use (from 15/17px); `type-page`/`type-display` grew
for more display presence; `space-6`/`space-7` grew (32→40px, 48→64px) for more generous page-level
whitespace; `radius-control`/`radius-panel` tightened (10→8px, 14→12px) for a crisper corner.
`tokens.test.ts`'s structural assertions — exactly seven type steps, seven space steps on a 4px
grid, exactly four radius roles — all still hold; only the pixel values inside those roles moved.

**Two centralised interaction states were added to `app.css`**: `:active` press feedback
(`transform: scale(0.98)`) on every button, and a `box-shadow` lift on hover for the three primary/
filled button variants. Both are additions to the existing element-level rules, so every screen
that already renders a `<button>` picked them up without a per-screen edit — the same leverage
ADR-055's element-selector approach was built for.

**Fonts, motion, layout, row, icon and z-index tokens are unchanged.** Source Sans 3 / Source Serif
4 remain self-hosted (ADR-061) rather than swapped: sourcing a new open-license variable font family
in this environment would mean a fresh network-fetched binary asset, and the pairing was already a
font "with character" rather than a browser default — the two problems a font swap exists to fix in
the general case don't apply here.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Audit ADR-055's existing system for gaps and inconsistencies, keep its palette | This was the first option offered, and explicitly not the one chosen — the request was for a full visual reset, understood to discard ADR-055's specific values. |
| Reuse ADR-055's "one HSL lightness, sweep ten hues" formula for the new client markers | Fails `contrast.test.ts`: a shared HSL lightness does not produce a shared relative luminance across hues, so several markers cleared 3:1 against white while failing it against the dark theme's near-black paper (and vice versa). Solving per-hue for a target luminance band is what ADR-055's own markers did too, on inspection — this ADR makes the method explicit rather than repeating the shortcut that broke. |
| An indigo/violet accent | The single most common visual fingerprint of "generic AI-generated product," per the redesign audit this ADR was produced against. A deep teal keeps the product's prior identity's hue family while moving it onto the new neutral ramp, and stays clearly distinct from the green `positive` status colour. |
| Swap Source Sans 3 / Source Serif 4 for a different self-hosted family (Geist, Outfit, etc.) | Would require fetching and vendoring a new binary font asset from this environment, which isn't reliably available here, for a benefit (font "has character") the existing self-hosted pairing already provides. Revisit if font files are supplied directly. |

## Consequences

- **Every screen re-skinned with zero per-screen edits**, because components reference only
  `var(--ledgr-*)` tokens (ADR-055's own design goal) — changing `primitives.ts`/`tokens.ts`/
  `tokens.css` cascades everywhere automatically. The only per-screen edits needed were two literal-
  colour exceptions that intentionally opt out of theming: `PreAuthScreen.css`'s marketing rail
  (ADR-057, a fixed brand statement) and the MFA TOTP QR panel's forced-white background (needs
  contrast for a QR scanner regardless of theme) — both were updated to the new dark-theme values by
  hand, consistent with why they were literals in the first place.
- **`contrast.test.ts` (92 assertions) and `tokens.test.ts` (150 assertions) both pass against the
  new values** — verified, not assumed. Two contrast failures surfaced on the first pass
  (`text-muted` on `surface-raised` at 4.40:1, needing 4.5:1; dark `attention-wash` against paper at
  1.03:1, needing >1.05) and were corrected before this ADR was written, not after.
- **`theme.test.tsx`'s literal `theme-color` assertions had to move with `index.html`** — a
  reminder that a few values are necessarily literals (an address bar can't read a CSS custom
  property) and drift silently if a palette change misses them.
- **What this forecloses**: client marker hex values are now load-bearing against a specific
  relative-luminance solving method, documented in `primitives.ts` — a future palette change to the
  neutrals must re-verify (not assume) that the ten markers still clear 3:1 against both themes'
  paper, since a neutral-ramp change shifts what "both themes' paper" even is.
- **Not done here**: no screenshot exists in this repo proving the rendered result, because no
  browser-automation tool (Playwright, chromium-cli) was available in the environment this was built
  in, and installing one — a new dependency plus a Chromium download — was treated as its own
  decision rather than taken silently. The dev server was confirmed to serve the new
  `tokens.css` correctly (byte-for-byte match against source), and the automated WCAG (axe-core)
  suite passed against real rendered DOM, but a human should open the app once before this is
  treated as verified in the way a screenshot would verify it.
