/**
 * @ledgr/i18n — the one place a user-facing string or a formatted figure
 * comes from, for the web app and the mobile apps (FR-LOC-001, MOB-016).
 *
 * The API has a mirror of this in apps/api/src/api/i18n/, reading the SAME
 * catalogue directory and implementing the same formatting rules against the
 * same case table (../formatting-cases.json). Two implementations, one set of
 * strings and one set of expected outputs.
 *
 * Start at ../catalogue/README.md for the catalogue's contract, locale.ts for
 * why formatting is a table rather than `Intl`, and language.ts for the split
 * between language (per user) and locale (per administration) that FR-LOC-002
 * turns on.
 */

export {
  DEFAULT_LANGUAGE,
  SUPPORTED_LANGUAGES,
  isLanguage,
  pluralCategory,
  primarySubtag,
  resolveLanguage,
} from "./language";
export type { Language, LanguageEnvironment } from "./language";

export {
  DEFAULT_FORMATTING_LOCALE,
  FORMATTING_LOCALES,
  LOCALES,
  isFormattingLocale,
  localeSpec,
} from "./locale";
export type { FormattingLocale, LocaleSpec } from "./locale";

export { MONEY_SCALE, FormattingError, formatDate, formatMoney, formatNumber } from "./format";
export type { DateStyle } from "./format";

export {
  GLOSSARY,
  MESSAGES,
  MissingMessageError,
  glossaryDefinition,
  glossaryTerm,
  hasMessage,
  translate,
} from "./catalogue";
export type {
  GlossaryTerm,
  MessageKey,
  MessageParams,
  MessageRecord,
  MessageText,
} from "./catalogue";

export { roleLabel, roleMessageKey } from "./roles";
export type { RoleLabelOptions } from "./roles";

export { LANGUAGE_STORAGE_KEY, readStoredLanguage, storeLanguage } from "./device";

export { I18nProvider, useI18n, useTranslate } from "./react";
export type { I18nContextValue, I18nProviderProps } from "./react";
