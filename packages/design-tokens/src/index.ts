/**
 * `@ledgr/design-tokens` — the single source of truth for how LEDGR looks.
 *
 * Consumers import `@ledgr/design-tokens/tokens.css` once, at the composition
 * root, and then reference `var(--ledgr-*)` from their own stylesheets. The
 * TypeScript exports here are for the handful of callers that need a colour as
 * a VALUE rather than as a style — `<meta name="theme-color">` is the one that
 * exists today.
 *
 * See ADR-055 for why this is a package rather than a stylesheet in the web
 * app, and why the CSS and the TypeScript are kept in sync by a test.
 */

export {
  AA_BODY_TEXT,
  AA_LARGE_TEXT,
  AA_NON_TEXT,
  contrastRatio,
  parseHex,
  relativeLuminance,
} from "./contrast";

export {
  CLIENT,
  NEUTRAL_DARK,
  NEUTRAL_LIGHT,
  ON_ACCENT_DARK,
  ON_ACCENT_LIGHT,
  ON_CLIENT_MARKER,
  STATUS_DARK,
  STATUS_LIGHT,
} from "./primitives";

export {
  applyTheme,
  browserThemeColour,
  readThemePreference,
  resolveTheme,
  systemPrefersDark,
  THEME_PREFERENCES,
  THEME_STORAGE_KEY,
  writeThemePreference,
  type ResolvedTheme,
  type ThemePreference,
} from "./theme";

export {
  clientTokens,
  CSS_PREFIX,
  cssVar,
  darkTheme,
  lightTheme,
  scaleTokens,
  themeValue,
  type ScaleTokenName,
  type ThemeTokenName,
  type ThemeTokens,
} from "./tokens";
