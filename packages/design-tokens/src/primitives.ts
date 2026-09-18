/**
 * The raw palette — every literal colour value in the product, in one place.
 *
 * Nothing outside this file may contain a hex colour. `tokens.ts` maps these
 * primitives onto SEMANTIC tokens ("the colour of a panel"), and components
 * only ever reference the semantic token. That indirection is the whole point:
 * a primitive answers "what colour is this", a semantic token answers "what is
 * this colour FOR", and only the second one survives a theme switch.
 *
 * --- ADR-065: a cool graphite ramp, not the warm hue-80 ramp ADR-055 shipped ---
 *
 * The neutral ramp is now a near-zero-chroma cool grey (the "graphite" family
 * most current SaaS dashboards use — Linear, Vercel, Stripe's own console),
 * replacing ADR-055's warm OKLCH-80 ramp. This is a deliberate visual reset,
 * not a correction: ADR-055's warm ramp was itself a considered choice. Every
 * value below is still picked the same way ADR-055's were — against
 * `contrast.test.ts`, not by eye — so the discipline carries over even though
 * the palette does not.
 *
 * --- Status colours: one lightness band per theme, hue is what differs ---
 *
 * As before, the four status colours (accent/positive/caution/attention) are
 * chosen so each clears AA body-text contrast (4.5:1) against all three
 * grounds in its own theme — not just the most common one. The light values
 * sit in the L 0.30–0.45 relative-luminance band; the dark values are lifted
 * into the L 0.55–0.75 band, because a mid-lightness hue that reads clearly on
 * white disappears against a near-black ground.
 *
 * --- Client colours: a fixed luminance band that works on BOTH themes' paper ---
 *
 * `CLIENT`'s ten keys and their ORDER are the `client_colour.position` order
 * from migration 0018 (see `apps/api/migrations/0018_client_switcher.sql`) —
 * the API stores and returns the NAME ("amber"), never a hex value, so the
 * ten keys themselves cannot be renamed or reordered without a migration.
 * Only the hex each name maps to changes here.
 *
 * A marker has to clear 3:1 against white AND against near-black at once,
 * which is a narrow relative-luminance band (roughly 0.14–0.20) regardless of
 * hue — bright hues like yellow-green reach that luminance at a much lower
 * HSL lightness than blue-violet does, so each of the ten was solved for its
 * own hue rather than sharing one lightness value.
 */

/** Cool graphite neutral ramp. Light theme reads this top-down. */
export const NEUTRAL_LIGHT = {
  ink: "#18181B",
  inkSecondary: "#3F3F46",
  inkMuted: "#6B6B74",
  /*
   * The boundary of a CONTROL (an input, a button's outline) — WCAG SC 1.4.11
   * wants 3:1 here. `rule` below is for dividers and table rules, which are
   * not controls and can stay closer to the ground.
   */
  ruleStrong: "#8B8B93",
  rule: "#E4E4E7",
  sunken: "#E4E4E7",
  raised: "#F4F4F5",
  ground: "#FAFAFA",
  paper: "#FFFFFF",
  input: "#FFFFFF",
} as const;

/** The dark graphite ramp — not an inversion of the light one; see status
 * colours below for why that shortcut does not survive contact with
 * `contrast.test.ts`. */
export const NEUTRAL_DARK = {
  ink: "#FAFAFA",
  inkSecondary: "#C7C7CC",
  inkMuted: "#9A9AA2",
  ruleStrong: "#6F6F78",
  rule: "#2C2C30",
  sunken: "#050506",
  raised: "#27272A",
  ground: "#09090B",
  paper: "#18181B",
  input: "#1C1C1F",
} as const;

/**
 * Status colours, light theme. A deep teal accent — not the indigo/violet
 * that reads as the generic "AI product" gradient hue — paired with a true
 * green for `positive` so the two are never mistaken for each other even
 * though both sit on the cool side of the wheel.
 *
 * Each has a `wash`: the same hue at very high lightness, for the background
 * of a row or chip carrying that state. A wash is never a text colour and the
 * solid is never a large background — they are not interchangeable.
 */
export const STATUS_LIGHT = {
  accent: "#0F766E",
  accentHover: "#0C5A54",
  accentWash: "#CCFBF1",
  positive: "#15803D",
  positiveWash: "#DCFCE7",
  caution: "#A3540C",
  cautionWash: "#FEF3C7",
  attention: "#B91C1C",
  attentionWash: "#FEE2E2",
} as const;

/**
 * Status colours, dark theme — lifted into a much higher lightness band than
 * the light values, the same way ADR-055's did: a colour that clears 4.5:1 on
 * white loses most of that margin against a near-black ground.
 * `contrast.test.ts` asserts the result rather than trusting this comment.
 */
export const STATUS_DARK = {
  accent: "#2DD4BF",
  accentHover: "#5EEAD4",
  accentWash: "#0F2E2C",
  positive: "#4ADE80",
  positiveWash: "#14251A",
  caution: "#FBBF24",
  cautionWash: "#2C2108",
  attention: "#F87171",
  attentionWash: "#331717",
} as const;

/**
 * The ten client marker colours, in `CLIENT_COLOURS` order (shared-types) —
 * see this file's header. Solved per-hue for a relative luminance around
 * 0.17, which is what lets the same ten values clear 3:1 against both a white
 * and a near-black paper.
 *
 * Ten is more than colour can actually carry: under deuteranopia several of
 * these collapse toward each other, which is why `ClientHeader` renders
 * initials and name alongside the marker and never the marker alone — colour
 * does recognition here, never identification.
 */
export const CLIENT = {
  indigo: "#6F68C1",
  amber: "#857136",
  teal: "#347F60",
  rose: "#B85263",
  lime: "#4D7E34",
  violet: "#9956BA",
  cyan: "#3E7997",
  orange: "#A36342",
  emerald: "#34814E",
  fuchsia: "#B54B92",
} as const;

/**
 * Initials on a client marker. Pure white in both themes, because the markers
 * themselves are theme-independent, and every one of the ten above was solved
 * to keep white legible on it (`contrast.test.ts`).
 */
export const ON_CLIENT_MARKER = "#FFFFFF";

/** Text on a filled accent surface — a primary button, a selected tab. */
export const ON_ACCENT_LIGHT = "#FFFFFF";

/**
 * Dark theme's accent is a light teal, so text on it must be dark. A teal-
 * tinted near-black rather than the ramp's plain `ground`, so it reads as
 * ink sitting on that specific surface rather than a neutral cutout.
 */
export const ON_ACCENT_DARK = "#052E2B";
