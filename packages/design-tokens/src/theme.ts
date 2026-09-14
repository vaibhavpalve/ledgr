import { darkTheme, lightTheme } from "./tokens";

/**
 * Theme preference: reading it, resolving it, and writing it to the document.
 *
 * Deliberately free of React — and of any framework — because this logic has
 * to run in two places that share no runtime:
 *
 *   1. The inline `<script>` in index.html, BEFORE first paint, so a dark-mode
 *      user never sees a white flash. That script cannot import a React hook.
 *   2. `useTheme` in the web app, when someone changes the setting.
 *
 * The inline script is a hand-written copy of `applyTheme` + `readPreference`
 * (it must be, to run before any module loads), so `theme.test.ts` asserts the
 * copy in index.html still agrees with this module. That is the only way the
 * duplication is safe.
 */

/**
 * Three states, not two. "system" is the DEFAULT and is not the same as
 * explicitly choosing the theme the system currently happens to be in: a user
 * on "system" follows their OS when it changes at sunset, one who chose
 * "light" does not.
 */
export type ThemePreference = "system" | "light" | "dark";

/** What a preference resolves to once the OS has been consulted. */
export type ResolvedTheme = "light" | "dark";

/**
 * Deliberately un-prefixed by environment: one key, so a preference set on
 * this device survives a deployment. Versioned in the name so a future change
 * of shape does not have to read a value it cannot parse.
 */
export const THEME_STORAGE_KEY = "ledgr.theme.v1";

export const THEME_PREFERENCES: readonly ThemePreference[] = ["system", "light", "dark"];

function isThemePreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

/**
 * What this device has chosen, or "system" when nothing has been chosen or the
 * stored value is unreadable.
 *
 * Every access is wrapped: `localStorage` THROWS rather than returning null in
 * a browser configured to block site data, and in a private window it can be
 * present but empty. A theme preference is never worth breaking a render over,
 * so any failure resolves to the default.
 */
export function readThemePreference(storage?: Pick<Storage, "getItem">): ThemePreference {
  try {
    const store = storage ?? globalThis.localStorage;
    const stored = store?.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

/** Persists a preference. Silent on failure, for the same reasons as above. */
export function writeThemePreference(
  preference: ThemePreference,
  storage?: Pick<Storage, "setItem" | "removeItem">,
): void {
  try {
    const store = storage ?? globalThis.localStorage;
    if (!store) return;
    // "system" is the absence of a choice, so it is stored as an absence
    // rather than as a third value — a user who returns to the default should
    // be indistinguishable from one who never chose.
    if (preference === "system") {
      store.removeItem(THEME_STORAGE_KEY);
    } else {
      store.setItem(THEME_STORAGE_KEY, preference);
    }
  } catch {
    /* A preference that could not be persisted still applies to this page. */
  }
}

/** True when the OS asks for dark. False when it asks for light, or is silent. */
export function systemPrefersDark(): boolean {
  try {
    return globalThis.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  } catch {
    return false;
  }
}

/** A preference plus the OS's answer → the theme actually being rendered. */
export function resolveTheme(preference: ThemePreference, prefersDark: boolean): ResolvedTheme {
  if (preference === "system") return prefersDark ? "dark" : "light";
  return preference;
}

/**
 * Writes the preference to the document element.
 *
 * "system" REMOVES the attribute rather than stamping the resolved value —
 * that is what lets `@media (prefers-color-scheme: dark)` in tokens.css do its
 * own work, including following the OS live when the user changes it while the
 * tab is open. Stamping the resolved value would freeze the page in whatever
 * the OS happened to be at load.
 */
export function applyTheme(preference: ThemePreference, root?: HTMLElement): void {
  const element = root ?? globalThis.document?.documentElement;
  if (!element) return;

  if (preference === "system") {
    element.removeAttribute("data-theme");
  } else {
    element.setAttribute("data-theme", preference);
  }
}

/**
 * The browser-chrome colour for a resolved theme — the address bar on Android,
 * the title bar of an installed PWA.
 *
 * `<meta name="theme-color">` cannot take a custom property, so this is one of
 * the few places that needs a resolved value rather than a `var()`. It reads
 * the ground rather than the accent: the chrome should continue the page, not
 * announce the brand, and an accent-coloured bar above a dark page is the
 * single most common PWA theming mistake.
 */
export function browserThemeColour(theme: ResolvedTheme): string {
  return (theme === "dark" ? darkTheme : lightTheme)["surface-ground"];
}
