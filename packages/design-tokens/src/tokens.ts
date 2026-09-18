import {
  CLIENT,
  NEUTRAL_DARK,
  NEUTRAL_LIGHT,
  ON_ACCENT_DARK,
  ON_ACCENT_LIGHT,
  ON_CLIENT_MARKER,
  STATUS_DARK,
  STATUS_LIGHT,
} from "./primitives";

/**
 * The semantic token layer — the only vocabulary components are allowed to
 * use, and the typed mirror of `tokens.css`.
 *
 * Two representations of the same facts exist on purpose:
 *
 *   tokens.css   what the browser actually applies, as custom properties.
 *   this file    what TypeScript can see, for the handful of places that need
 *                a colour as a VALUE rather than as a style — a `<meta
 *                name="theme-color">`, a canvas fill, an inline SVG stop.
 *
 * They can drift, so `tokens.test.ts` parses the CSS and asserts every token
 * here appears there with the same value in both themes. That is the same
 * shape as `shared-types`' own `index.test.ts`, which parses migration 0018
 * rather than trusting a comment that says the two agree.
 *
 * --- Naming ---
 *
 * `--ledgr-<group>-<role>`. The group says what kind of thing it is, the role
 * says what it is for. A token never names a colour ("teal", "grey") because
 * the whole point is that the value changes between themes and the role does
 * not.
 */

/** A theme is exactly this set of keys — both themes must define all of them. */
export interface ThemeTokens {
  /* Surfaces, back to front. */
  "surface-ground": string;
  "surface-paper": string;
  "surface-raised": string;
  "surface-sunken": string;
  "surface-input": string;
  /* The backdrop behind a dialog or drawer — the one translucent surface. */
  "surface-scrim": string;

  /* Lines. `border-focus` is the ring colour; see `focus-ring` below. */
  "border-subtle": string;
  "border-strong": string;
  "border-focus": string;

  /* Text, by prominence. Every one clears 4.5:1 on ground, paper and raised. */
  "text-primary": string;
  "text-secondary": string;
  "text-muted": string;
  "text-on-accent": string;
  "text-on-marker": string;

  /* Status. A `-wash` is a background only; the solid is a text/icon colour. */
  accent: string;
  "accent-hover": string;
  "accent-wash": string;
  positive: string;
  "positive-wash": string;
  caution: string;
  "caution-wash": string;
  attention: string;
  "attention-wash": string;

  /* Elevation. Dark themes need more opacity to read at all. */
  "shadow-sm": string;
  "shadow-md": string;
  "shadow-lg": string;
}

export const lightTheme: ThemeTokens = {
  "surface-ground": NEUTRAL_LIGHT.ground,
  "surface-paper": NEUTRAL_LIGHT.paper,
  "surface-raised": NEUTRAL_LIGHT.raised,
  "surface-sunken": NEUTRAL_LIGHT.sunken,
  "surface-input": NEUTRAL_LIGHT.input,
  /* NEUTRAL_LIGHT.ink at 48%: dimmed paper, not a grey film. */
  "surface-scrim": "rgba(24, 24, 27, 0.48)",

  "border-subtle": NEUTRAL_LIGHT.rule,
  "border-strong": NEUTRAL_LIGHT.ruleStrong,
  "border-focus": STATUS_LIGHT.accent,

  "text-primary": NEUTRAL_LIGHT.ink,
  "text-secondary": NEUTRAL_LIGHT.inkSecondary,
  "text-muted": NEUTRAL_LIGHT.inkMuted,
  "text-on-accent": ON_ACCENT_LIGHT,
  "text-on-marker": ON_CLIENT_MARKER,

  accent: STATUS_LIGHT.accent,
  "accent-hover": STATUS_LIGHT.accentHover,
  "accent-wash": STATUS_LIGHT.accentWash,
  positive: STATUS_LIGHT.positive,
  "positive-wash": STATUS_LIGHT.positiveWash,
  caution: STATUS_LIGHT.caution,
  "caution-wash": STATUS_LIGHT.cautionWash,
  attention: STATUS_LIGHT.attention,
  "attention-wash": STATUS_LIGHT.attentionWash,

  "shadow-sm": "0 1px 2px rgba(24, 24, 27, 0.06)",
  "shadow-md": "0 2px 4px rgba(24, 24, 27, 0.05), 0 8px 20px -10px rgba(24, 24, 27, 0.16)",
  "shadow-lg": "0 4px 8px rgba(24, 24, 27, 0.06), 0 24px 48px -20px rgba(24, 24, 27, 0.28)",
};

export const darkTheme: ThemeTokens = {
  "surface-ground": NEUTRAL_DARK.ground,
  "surface-paper": NEUTRAL_DARK.paper,
  "surface-raised": NEUTRAL_DARK.raised,
  "surface-sunken": NEUTRAL_DARK.sunken,
  "surface-input": NEUTRAL_DARK.input,
  /* Pure black rather than NEUTRAL_DARK.ink: a scrim this large reads as
     depth better without picking up the ramp's own (slight) tint. */
  "surface-scrim": "rgba(0, 0, 0, 0.6)",

  "border-subtle": NEUTRAL_DARK.rule,
  "border-strong": NEUTRAL_DARK.ruleStrong,
  "border-focus": STATUS_DARK.accent,

  "text-primary": NEUTRAL_DARK.ink,
  "text-secondary": NEUTRAL_DARK.inkSecondary,
  "text-muted": NEUTRAL_DARK.inkMuted,
  "text-on-accent": ON_ACCENT_DARK,
  "text-on-marker": ON_CLIENT_MARKER,

  accent: STATUS_DARK.accent,
  "accent-hover": STATUS_DARK.accentHover,
  "accent-wash": STATUS_DARK.accentWash,
  positive: STATUS_DARK.positive,
  "positive-wash": STATUS_DARK.positiveWash,
  caution: STATUS_DARK.caution,
  "caution-wash": STATUS_DARK.cautionWash,
  attention: STATUS_DARK.attention,
  "attention-wash": STATUS_DARK.attentionWash,

  /* Black rather than NEUTRAL_DARK.ink, for the same reason as the scrim
     above: depth reads better than a hue-matched tint at this size. */
  "shadow-sm": "0 1px 2px rgba(0, 0, 0, 0.4)",
  "shadow-md": "0 2px 4px rgba(0, 0, 0, 0.4), 0 8px 20px -10px rgba(0, 0, 0, 0.6)",
  "shadow-lg": "0 4px 8px rgba(0, 0, 0, 0.5), 0 24px 48px -20px rgba(0, 0, 0, 0.75)",
};

/** Client marker colours — one set, both themes (see primitives.ts). */
export const clientTokens = CLIENT;

/**
 * Everything that does not change between themes: the type scale, the spacing
 * grid, radii, motion.
 *
 * --- Seven type steps, not fifteen ---
 *
 * The design canvas had fifteen sizes in use, several pairs a single pixel
 * apart doing the same job. A reader cannot perceive a 1px step as hierarchy —
 * they perceive it as inconsistency, which is the difference between an
 * interface that reads as designed and one that reads as assembled. Seven
 * steps, each a clear jump from the last.
 *
 * The two largest are fluid, because they are the only ones whose job changes
 * with the viewport: a page title and a monetary figure should fill a desktop
 * header and still fit a 360px phone without wrapping. Everything at body size
 * and below stays fixed — fluid body text is a readability regression, not a
 * modern touch.
 *
 * --- A four-pixel grid, and four radii by role ---
 *
 * Twenty gap values and twelve radii were in use. Nothing was wrong in
 * isolation; collectively it meant no two panels shared a rhythm. Radius now
 * says what KIND of object something is — a chip is not a panel — which is
 * information it could not carry while every value was taken.
 */
export const scaleTokens = {
  /* Type. `--ledgr-type-*`. ADR-065 moved body/small down to the 14/16px
     convention most current dashboards read as "designed", and gave the two
     fluid steps more presence — a bigger jump reads as more confident, not
     just bigger. */
  "type-micro": "0.75rem" /* 12px — uppercase labels, keycaps */,
  "type-caption": "0.8125rem" /* 13px — captions, helper text */,
  "type-small": "0.875rem" /* 14px — secondary UI, table meta */,
  "type-body": "1rem" /* 16px — body and default UI */,
  "type-section": "1.25rem" /* 20px — section headings */,
  "type-page": "clamp(1.75rem, 1.5rem + 1vw, 2rem)" /* 28→32px */,
  "type-display": "clamp(2rem, 1.5rem + 2.5vw, 2.75rem)" /* 32→44px */,

  /* Line heights, by role rather than by size. */
  "leading-tight": "1.2",
  "leading-snug": "1.35",
  "leading-normal": "1.55",

  /* Weights. Four, and 500 exists so a label can be distinct from body
     without jumping to the 600 used for genuine emphasis. */
  "weight-regular": "400",
  "weight-medium": "500",
  "weight-semibold": "600",
  "weight-bold": "700",

  /* Families. Source Serif 4 carries figures and page titles; nobody else in
     the Dutch bookkeeping market sets monetary amounts in a serif, and it is
     the single cheapest thing that makes a screenshot recognisable. */
  "font-sans": "'Source Sans 3', 'Segoe UI', system-ui, -apple-system, sans-serif",
  "font-serif": "'Source Serif 4', Georgia, 'Times New Roman', serif",
  "font-mono": "ui-monospace, 'Cascadia Mono', 'SF Mono', Consolas, monospace",

  /* Spacing — a 4px grid. The two largest steps (page-level gaps, empty-state
     padding) grew under ADR-065: whitespace is one of the cheapest signals
     of "premium", and it costs nothing at the component level below. */
  "space-1": "0.25rem" /* 4 */,
  "space-2": "0.5rem" /* 8 */,
  "space-3": "0.75rem" /* 12 */,
  "space-4": "1rem" /* 16 */,
  "space-5": "1.5rem" /* 24 */,
  "space-6": "2.5rem" /* 40 */,
  "space-7": "4rem" /* 64 */,

  /* Radii, by role. Tighter than ADR-055's under ADR-065 — a crisper corner
     reads as more precise on a numbers-first product than a soft one does. */
  "radius-chip": "6px",
  "radius-control": "8px",
  "radius-panel": "12px",
  "radius-pill": "999px",

  /* Motion. A ledger should move almost not at all; what does move must be
     fast enough to feel like a response rather than an animation. Every use
     is wrapped in a `prefers-reduced-motion` guard in tokens.css. */
  "duration-fast": "120ms",
  "duration-base": "200ms",
  "duration-slow": "320ms",
  "ease-out": "cubic-bezier(0.2, 0, 0, 1)",
  "ease-spring": "cubic-bezier(0.2, 0.9, 0.3, 1.1)",

  /* Touch targets. MOB-013's floor, as a token so no screen re-derives it. */
  "touch-min": "48px",

  /* Layout (docs/design/system.md §2): the desktop shell's fixed dimensions.
     The 64rem desktop breakpoint itself is not here — a custom property
     cannot be used inside a media query. */
  "layout-rail": "15rem" /* 240 — the left rail, Main.dc.html */,
  "layout-bar": "4rem" /* 64 — the client bar */,
  "layout-measure": "72rem" /* content max width, MobileShell.css */,
  "layout-form": "40rem" /* a single-column form */,
  "layout-drawer": "30rem" /* a side drawer beside a list */,

  /* Table and list rows at the two densities (`<html data-density>`). */
  "row-comfortable": "3rem" /* 48 — the touch floor */,
  "row-compact": "2.5rem" /* 40 — desktop only */,

  /* The three sizes the 20-grid icons render at (stroke scaled to 1.5px). */
  "icon-sm": "16px",
  "icon-md": "20px",
  "icon-lg": "24px",

  /* Stacking, in order. Every z-index in the product is one of these. */
  "z-nav": "10",
  "z-header": "20",
  "z-overlay": "50",
  "z-toast": "60",
  "z-skip": "100",
} as const;

export type ScaleTokenName = keyof typeof scaleTokens;
export type ThemeTokenName = keyof ThemeTokens;

/** `cssVar("accent")` → `"var(--ledgr-accent)"`. For inline styles. */
export function cssVar(name: ThemeTokenName | ScaleTokenName): string {
  return `var(--${CSS_PREFIX}-${name})`;
}

export const CSS_PREFIX = "ledgr";

/**
 * The resolved value of a theme token, for the few callers that need a real
 * colour rather than a `var()` — `<meta name="theme-color">` is the one that
 * exists today, since a browser chrome colour cannot be a custom property.
 */
export function themeValue(theme: "light" | "dark", name: ThemeTokenName): string {
  return (theme === "dark" ? darkTheme : lightTheme)[name];
}
