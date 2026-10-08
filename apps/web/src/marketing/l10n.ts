import { useCallback } from "react";
import { useI18n, type Language } from "@ledgr/i18n";

/**
 * Marketing copy, in both languages side by side (ADR-107).
 *
 * The product's interface strings live in the catalogue (`packages/i18n`), one short key each.
 * The public site is different in kind: long-form pages and articles, where a paragraph is the
 * unit and reading the Dutch next to the English is how a translation is checked. So its copy is
 * typed data: every string is an `L`, and TypeScript refuses one without its Dutch half. The
 * language itself is the app's own (`useI18n`), so the site and the sign-in screen agree.
 */
export interface L {
  readonly en: string;
  readonly nl: string;
}

export function useLang(): Language {
  return useI18n().language;
}

/** Picks the reader's half of a bilingual string. */
export function useL(): (text: L) => string {
  const language = useLang();
  return useCallback((text: L) => (language === "nl" ? text.nl : text.en), [language]);
}
