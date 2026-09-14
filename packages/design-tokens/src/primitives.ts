/**
 * The raw palette — every literal colour value in the product, in one place.
 *
 * Nothing outside this file may contain a hex colour. `tokens.ts` maps these
 * primitives onto SEMANTIC tokens ("the colour of a panel"), and components
 * only ever reference the semantic token. That indirection is the whole point:
 * a primitive answers "what colour is this", a semantic token answers "what is
 * this colour FOR", and only the second one survives a theme switch.
 *
 * --- One hue through the entire neutral ramp ---
 *
 * Every neutral below sits on OKLCH hue 80 — warm, all the way down, from
 * `INK` to `PAPER`. This is deliberate and it is the product's signature: the
 * common failure in finance UIs is a cool-leaning grey (blue ink) over a warm
 * ground, and that hue flip is exactly what makes mid-greys read as dirty.
 * Holding one hue costs nothing and is visible on every screen.
 *
 * The dark ramp is the SAME hue at inverted lightness, which is why it was
 * cheap to add: it is not a second palette, it is the same ramp read from the
 * other end.
 *
 * --- Semantics: one lightness, one chroma, hue is the only variable ---
 *
 * The four status colours sit at L 0.47 / C 0.08 in the light theme and differ
 * only in hue: accent 200, positive 148, caution 75, attention 38. C 0.08 is
 * the highest chroma all four hues can hold inside sRGB — above it two of them
 * clip and the set stops being uniform, so one status would read as louder
 * than another for a reason that carries no meaning.
 *
 * --- Client colours do not change with the theme ---
 *
 * `CLIENT` is one set, used in both themes, and that is a correctness decision
 * rather than an oversight: a client's marker colour is that client's
 * identity (FR-FRM-000a), and an identity that looked different at night
 * would defeat the signal it exists to carry. The ten sit at L 0.55 / C 0.07 —
 * deliberately quieter than the semantics above, so a client marker never
 * shouts louder than a status, and mid-toned enough to hold white initials on
 * either ground.
 */

/** Warm neutral ramp, OKLCH hue 80. Light theme reads this top-down. */
export const NEUTRAL_LIGHT = {
  ink: "#26221C",
  inkSecondary: "#5F5A52",
  inkMuted: "#6E685F",
  /*
   * Darker than the design canvas's #C1BDB7, and the change was forced by
   * `contrast.test.ts` rather than chosen: this is the border of an INPUT, and
   * WCAG SC 1.4.11 wants the visual boundary of a control to clear 3:1 against
   * its background. #C1BDB7 managed about 1.6:1 on paper — a boundary a
   * low-vision user cannot find. Dividers and table rules are not controls and
   * keep using `rule` below, so nothing gets heavier except the things you are
   * meant to be able to click.
   */
  ruleStrong: "#8F8878",
  rule: "#DCD9D3",
  sunken: "#E3DED5",
  raised: "#EFEAE2",
  ground: "#F5F0E9",
  paper: "#FBF8F4",
  input: "#FFFEFB",
} as const;

/** The same hue 80 ramp, inverted. Not a second palette — the same one. */
export const NEUTRAL_DARK = {
  ink: "#F2ECE3",
  inkSecondary: "#C3BBAE",
  inkMuted: "#9A9285",
  /* Same SC 1.4.11 reasoning as the light ramp's `ruleStrong`, measured
     against the dark paper rather than the light one. */
  ruleStrong: "#756D5F",
  rule: "#3D372F",
  sunken: "#100D0A",
  raised: "#2A251F",
  ground: "#15120E",
  paper: "#201C17",
  input: "#1A1712",
} as const;

/**
 * Status colours, light theme. L 0.47 / C 0.08 — see this file's header for
 * why the chroma is pinned there rather than pushed higher.
 *
 * Each has a `wash`: the same hue at very high lightness, for the background
 * of a row or chip carrying that state. A wash is never a text colour and the
 * solid is never a large background — they are not interchangeable.
 */
export const STATUS_LIGHT = {
  accent: "#00686C",
  accentHover: "#004F52",
  accentWash: "#D8F4F6",
  positive: "#396741",
  positiveWash: "#E0EFE2",
  caution: "#755421",
  cautionWash: "#FAEBD8",
  attention: "#814A39",
  attentionWash: "#FEE8E1",
} as const;

/**
 * Status colours, dark theme.
 *
 * These are NOT the light values inverted. On a dark ground a mid-lightness
 * hue loses contrast against the surface while gaining it against nothing
 * useful, so each solid is lifted to roughly L 0.78 and each wash dropped to
 * roughly L 0.22. `contrast.test.ts` asserts the result rather than trusting
 * this comment.
 */
export const STATUS_DARK = {
  accent: "#4FBCC0",
  accentHover: "#7FD4D7",
  accentWash: "#10312F",
  positive: "#9CC6A1",
  positiveWash: "#1B2E1D",
  caution: "#D9B47C",
  cautionWash: "#33270F",
  attention: "#E0A18C",
  attentionWash: "#3A241C",
} as const;

/**
 * The ten client marker colours, in `CLIENT_COLOURS` order (shared-types),
 * which is `client_colour.position` order from migration 0018 — the tie-break
 * the allocation trigger uses, so this array's ORDER is load-bearing and
 * `tokens.test.ts` asserts it against the shared type.
 *
 * Ten hues over a 300-degree arc with a 30-degree dead zone either side of the
 * accent, so no client is ever mistaken for the interface's own colour.
 *
 * Ten is more than colour can actually carry: under deuteranopia these
 * collapse to about five, and amber/lime become literally identical. That is
 * why `ClientHeader` renders initials and name alongside the marker and never
 * the marker alone — colour does recognition here, never identification.
 */
export const CLIENT = {
  indigo: "#59739B",
  amber: "#8E6944",
  teal: "#477F68",
  rose: "#936071",
  lime: "#7C7241",
  violet: "#736A97",
  cyan: "#407A91",
  orange: "#966258",
  emerald: "#617B50",
  fuchsia: "#876388",
} as const;

/**
 * Initials on a client marker. Pure white in both themes, because the markers
 * themselves are theme-independent: the off-white `PAPER` tone only reaches
 * 4.42:1 on the darkest client colour, where white clears 4.5:1 on all ten.
 */
export const ON_CLIENT_MARKER = "#FFFFFF";

/** Text on a filled accent surface — a primary button, a selected tab. */
export const ON_ACCENT_LIGHT = "#FFFFFF";

/**
 * Dark theme's accent is light, so text on it must be dark. Near-black rather
 * than the ramp's `ground`, because this sits on a saturated teal and picks up
 * a green cast from a neutral that low in chroma.
 */
export const ON_ACCENT_DARK = "#0A1F20";
