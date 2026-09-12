/**
 * The React binding — FR-LOC-001a, FR-LOC-001b, FR-LOC-002.
 *
 *   "Language is ... changeable at any time from the user menu in one click,
 *    taking effect immediately without reload or re-authentication."
 *
 * That sentence is a statement about state, and it is what shapes this file.
 * Language is React state held here, so changing it re-renders and the new
 * words are on screen in the same frame. Persisting the choice — to the
 * device and to the account — happens AFTER, through an injected callback,
 * and the UI never waits for it. A switcher that awaited a round trip before
 * repainting would take effect on the network's schedule, and one that
 * reloaded to apply the choice would lose whatever was half-typed on screen.
 *
 * --- Two values, not one ---
 *
 * The context carries a `language` and a `formattingLocale` and never derives
 * one from the other (FR-LOC-002). `t()` reads the language; `money()`,
 * `number()` and `date()` read the locale. A component asking for both is
 * asking two different questions, and the hook is shaped so it cannot
 * accidentally answer them from one setting.
 *
 * `formattingLocale` comes from the administration the session has open, so
 * switching client changes how amounts are written and does not change a word
 * of the UI — which is exactly the behaviour FR-LOC-002 describes.
 */

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type { MessageKey, MessageParams } from "./catalogue";
import { glossaryDefinition, translate } from "./catalogue";
import type { DateStyle } from "./format";
import { formatDate, formatMoney, formatNumber } from "./format";
import type { Language } from "./language";
import type { FormattingLocale } from "./locale";
import { DEFAULT_FORMATTING_LOCALE } from "./locale";

export interface I18nContextValue {
  /** What the reader reads. Per user (FR-LOC-001b). */
  readonly language: Language;
  /** How figures are written. Per administration (FR-LOC-002). */
  readonly formattingLocale: FormattingLocale;
  /**
   * The reader CHOSE this. Immediate, synchronous, no reload (FR-LOC-001a),
   * and persisted through `onLanguageChange`.
   */
  setLanguage(next: Language): void;
  /**
   * The reader's ACCOUNT already said this — adopt it without telling the
   * account again (IAM-010g, at first login).
   *
   * Separate from `setLanguage` because a choice and an adoption are
   * different events, and treating the second as the first means writing a
   * value straight back to the place it was just read from: one pointless
   * round trip per sign-in, on every machine whose remembered language
   * differs from the account's.
   *
   * Repaints exactly as `setLanguage` does. What it skips is the
   * notification, not the effect.
   */
  adoptLanguage(next: Language): void;
  t(key: MessageKey, params?: MessageParams): string;
  money(amount: string): string;
  number(value: string, options: { scale: number }): string;
  date(isoDate: string, style?: DateStyle): string;
  /** FR-LOC-001c's hover or tap text, or undefined for an unknown term. */
  glossary(termId: string): string | undefined;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export interface I18nProviderProps {
  /**
   * Resolved by `resolveLanguage` (IAM-010g) before the tree mounts, so the
   * first paint is already in the right language. Passing it in rather than
   * resolving here keeps this component free of `navigator` and `location`.
   */
  initialLanguage: Language;
  /** The open administration's locale; the default when no client is open. */
  formattingLocale?: FormattingLocale;
  /**
   * Called after the UI has already switched. Persist here: device storage
   * (IAM-010g) and `PUT /v1/me/language` (FR-LOC-001b). Injected rather than
   * called directly so this component needs no fetch and no storage, and so
   * a test can assert what was persisted without a network.
   *
   * Failure is the caller's to handle. It must not roll the UI back: the
   * person asked for English, they are looking at English, and undoing that
   * because a write failed would be a worse outcome than a preference that
   * does not survive the session.
   */
  onLanguageChange?: (next: Language) => void;
  children: ReactNode;
}

export function I18nProvider({
  initialLanguage,
  formattingLocale = DEFAULT_FORMATTING_LOCALE,
  onLanguageChange,
  children,
}: I18nProviderProps) {
  const [language, setLanguageState] = useState<Language>(initialLanguage);

  const setLanguage = useCallback(
    (next: Language) => {
      setLanguageState(next);
      onLanguageChange?.(next);
    },
    [onLanguageChange],
  );

  // No callback: the caller that adopts an account language owns whatever
  // caching it wants to do (the web app writes it to the device), and the one
  // thing it must not do is notify the account.
  const adoptLanguage = useCallback((next: Language) => setLanguageState(next), []);

  const value = useMemo<I18nContextValue>(
    () => ({
      language,
      formattingLocale,
      setLanguage,
      adoptLanguage,
      t: (key, params) => translate(key, language, params),
      money: (amount) => formatMoney(amount, formattingLocale),
      number: (amount, options) => formatNumber(amount, formattingLocale, options),
      date: (isoDate, style) => formatDate(isoDate, formattingLocale, style),
      glossary: (termId) => glossaryDefinition(termId, language),
    }),
    [language, formattingLocale, setLanguage, adoptLanguage],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

/**
 * Throws outside a provider rather than falling back to a default language.
 *
 * A default would make an unwrapped subtree render in Dutch regardless of
 * what the user chose, and it would do it silently — which is the same class
 * of failure as falling back to English for a missing string, and is ruled
 * out for the same reason (FR-LOC-001).
 */
export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (value === null) {
    throw new Error(
      "useI18n() outside an <I18nProvider>. Every user-facing string goes " +
        "through the catalogue (FR-LOC-001), so a component rendering text " +
        "must be inside one.",
    );
  }
  return value;
}

/** `const t = useTranslate()` for components that only need words. */
export function useTranslate(): I18nContextValue["t"] {
  return useI18n().t;
}
