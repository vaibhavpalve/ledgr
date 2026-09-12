/**
 * Which language a person reads the product in — FR-LOC-001, FR-LOC-001a,
 * FR-LOC-001b, IAM-010g, FR-ONB-000.
 *
 * --- Language is not locale, and this file is only about language ---
 *
 * FR-LOC-002 splits what most i18n libraries fuse into one setting:
 *
 *   LANGUAGE   which words a person reads. Per USER (FR-LOC-001b), so a Dutch
 *              bookkeeper and an English-speaking owner can sit in the same
 *              administration each in their own.
 *   LOCALE     how numbers, dates and money are written. Per ADMINISTRATION
 *              (FR-LOC-002), so an amount reads identically to both of them.
 *
 * Keeping them apart is the whole reason `Language` and `FormattingLocale`
 * (locale.ts) are separate types rather than one string. A single "locale"
 * setting cannot express "English words, Dutch numbers", which is the exact
 * case the requirement is written about, and a codebase that starts with one
 * string never gets the second one back without touching every call site.
 */

/**
 * The languages LEDGR ships. Both first-class, and the order of this array is
 * not a ranking — it is the order a language picker lists them in, which is
 * alphabetical by endonym (English, Nederlands) so neither language is
 * presented as the default one.
 */
export const SUPPORTED_LANGUAGES = ["en", "nl"] as const;

export type Language = (typeof SUPPORTED_LANGUAGES)[number];

/**
 * Where resolution lands when nothing else answers.
 *
 * PRD open question Q12 ("Is Dutch or English the default UI language for a
 * firm's staff users, given many Dutch firms work bilingually?") is
 * unresolved, and this constant is the answer the product ships until it is.
 * Dutch, because §20 already commits to Dutch for `.nl` traffic and the
 * product's market is Dutch SMBs — but this is one line, deliberately, so
 * that answering Q12 the other way is a one-line change rather than an
 * archaeology exercise across the codebase.
 *
 * Note what this is NOT: a fallback for a missing translation. FR-LOC-001 is
 * explicit that a missing translation is a release blocker, never a fallback
 * to English, and nothing in this package falls back between languages —
 * `translate` throws instead. This constant only answers "we have never been
 * told which language this person reads."
 */
export const DEFAULT_LANGUAGE: Language = "nl";

export function isLanguage(value: unknown): value is Language {
  return typeof value === "string" && (SUPPORTED_LANGUAGES as readonly string[]).includes(value);
}

/**
 * The subset of the browser this resolution actually depends on, named
 * explicitly so the rule can be tested without a DOM and so it is obvious
 * what a mobile client has to supply instead (MOB-016: the app's language is
 * independent of the device language if the user prefers, which is the
 * `storedChoice` field below).
 */
export interface LanguageEnvironment {
  /** A previous explicit choice, persisted on this device (IAM-010g). */
  storedChoice?: string | null;
  /** `navigator.languages`, most-preferred first. BCP 47 tags. */
  preferredLanguages?: readonly string[];
  /** The host the app is being served from, for the `.nl` rule. */
  hostname?: string | null;
  /** The signed-in user's stored setting, once there is one. */
  accountLanguage?: string | null;
}

/**
 * IAM-010g and FR-ONB-000: "selectable before authentication, on the login and
 * signup screens, defaulting to the browser or device locale and falling back
 * to Dutch for `.nl` traffic. The choice persists on the device and is applied
 * to the account after first login."
 *
 * The order below is that sentence, read backwards from most-specific:
 *
 *   1. accountLanguage  the signed-in user's own setting. Authoritative once
 *                       it exists, because FR-LOC-001b makes language a
 *                       property of the person, not of the device they
 *                       happen to be at — the same person on a borrowed
 *                       laptop should not switch language.
 *   2. storedChoice     an explicit choice made on this device. Outranks the
 *                       browser because it IS the person overriding the
 *                       browser, and it is what seeds the account on first
 *                       login.
 *   3. preferredLanguages  the browser or device locale. Matched on the
 *                       primary subtag, so `nl-BE` and `en-US` both resolve.
 *   4. hostname .nl     Dutch. Deliberately below the browser: someone whose
 *                       browser says English asked for English, and the
 *                       domain they arrived on does not know better.
 *   5. DEFAULT_LANGUAGE
 *
 * Pure, and given everything it needs. A resolution rule that read
 * `navigator` and `location` itself would be untestable in exactly the cases
 * worth testing.
 */
export function resolveLanguage(environment: LanguageEnvironment): Language {
  const { storedChoice, preferredLanguages, hostname, accountLanguage } = environment;

  if (isLanguage(accountLanguage)) return accountLanguage;
  if (isLanguage(storedChoice)) return storedChoice;

  for (const tag of preferredLanguages ?? []) {
    const primary = primarySubtag(tag);
    if (isLanguage(primary)) return primary;
  }

  if (typeof hostname === "string" && hostname.toLowerCase().endsWith(".nl")) {
    return "nl";
  }

  return DEFAULT_LANGUAGE;
}

/**
 * `nl-BE` → `nl`. BCP 47 tags are case-insensitive in the primary subtag and
 * may be separated by `-` or (in some older `Accept-Language` headers) `_`.
 */
export function primarySubtag(tag: string): string {
  return tag.trim().toLowerCase().split(/[-_]/)[0] ?? "";
}

/**
 * CLDR plural category. Dutch and English share one rule, so this is a single
 * branch today and is still written per-language.
 *
 * The reason is FR-LOC-005: adding a jurisdiction must not fork the codebase.
 * A counted message written as `count === 1 ? a : b` at every call site has to
 * be rewritten in full for the first language with a `few`; one written
 * against this function needs a case added here and new forms in the
 * catalogue.
 *
 * CLDR's rule for both languages is `i = 1 and v = 0` — integer part 1, and no
 * VISIBLE decimals, so "1,0 regel" is `other`. The second half is not
 * evaluable here and is not evaluable in Python either: a JS `number` and a
 * Python `float` both lose the difference between `1` and `1.0` before this
 * function sees them. So what is implemented is the half that survives — the
 * value is one — identically on both sides (api.i18n.language.plural_category).
 * A caller that genuinely needs to say "1,0" is formatting a number, and
 * should pass the count it is displaying rather than the value it holds.
 */
export function pluralCategory(language: Language, count: number): "one" | "other" {
  switch (language) {
    case "nl":
    case "en":
      return count === 1 ? "one" : "other";
  }
}
