import { useCallback, useEffect, useState } from "react";
import {
  applyTheme,
  readThemePreference,
  resolveTheme,
  systemPrefersDark,
  writeThemePreference,
  type ResolvedTheme,
  type ThemePreference,
} from "@ledgr/design-tokens";

/**
 * The theme preference, as React state (ADR-055).
 *
 * The DOM is the source of truth for what is being rendered — the inline
 * script in index.html has already stamped `data-theme` before this hook ever
 * runs — so this hook's job is not to apply the theme on mount but to keep
 * the stamp, the stored preference and the React tree agreeing after a change.
 *
 * --- Why `resolved` is tracked separately ---
 *
 * `preference` is what the user chose; `resolved` is what they are actually
 * looking at. They differ for everyone on the default "system" setting, and
 * the interface needs both: the control must show "System" as selected, while
 * the icon beside it shows whether that currently means light or dark.
 *
 * --- Why the media query is subscribed to ---
 *
 * On "system", tokens.css follows the OS on its own — no JavaScript needed for
 * the colours to change at sunset. But `resolved` would go stale, so the label
 * would keep claiming "light" against a dark screen. The listener exists for
 * the label, not for the palette.
 */
export function useTheme(): {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (next: ThemePreference) => void;
} {
  // Read once, lazily, rather than in an effect: an effect would render the
  // control showing "System" for a frame before snapping to the real choice.
  const [preference, setPreferenceState] = useState<ThemePreference>(() => readThemePreference());
  const [prefersDark, setPrefersDark] = useState<boolean>(() => systemPrefersDark());

  useEffect(() => {
    // `matchMedia` is absent in jsdom unless a test provides it, and absent in
    // any non-browser render. Nothing here is required for correctness — the
    // stylesheet has already handled the palette — so it degrades silently.
    const query = globalThis.matchMedia?.("(prefers-color-scheme: dark)");
    if (!query) return;

    const onChange = (event: MediaQueryListEvent) => setPrefersDark(event.matches);

    // `addEventListener` on a MediaQueryList is the modern spelling; Safari
    // below 14 only has `addListener`. Feature-detected rather than assumed,
    // because this is exactly the kind of call that throws on an older iPad
    // someone is using to photograph receipts.
    if (typeof query.addEventListener === "function") {
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    }
    return undefined;
  }, []);

  const setPreference = useCallback((next: ThemePreference) => {
    // Applied to the document FIRST, so the repaint happens on this frame and
    // is not waiting on either the state update or the storage write. Same
    // posture as `persistLanguage` (FR-LOC-001a): persisting is a side effect
    // of the switch, never a precondition for it.
    applyTheme(next);
    writeThemePreference(next);
    setPreferenceState(next);
  }, []);

  return {
    preference,
    resolved: resolveTheme(preference, prefersDark),
    setPreference,
  };
}
