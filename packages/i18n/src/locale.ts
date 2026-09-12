/**
 * Formatting locales — FR-LOC-002, FR-LOC-005.
 *
 *   "Locale-correct number, date and currency formatting; European decimal
 *    comma. Formatting follows the administration's locale, not the user's UI
 *    language, so amounts read identically to every user."
 *
 * A locale here belongs to an ADMINISTRATION, never to a person. That is the
 * requirement's whole point: an English-speaking owner and a Dutch bookkeeper
 * looking at the same trial balance must see the same characters, because a
 * figure that renders as `1,234.56` to one of them and `1.234,56` to the
 * other is two people reading two different numbers off one ledger — and
 * `1.234` means one thousand two hundred and thirty-four to one of them and
 * one-point-two-three-four to the other.
 *
 * --- Why this is a table and not Intl.NumberFormat ---
 *
 * `Intl` is the obvious answer and is the wrong one here, for three reasons
 * that all come back to the same requirement:
 *
 *   1. Its output is ICU's, and ICU changes. The space between a currency
 *      symbol and its digits changed character in ICU 72; grouping and
 *      symbol placement have moved for other locales in other releases. A
 *      figure whose rendering depends on the browser version is not one that
 *      "reads identically to every user" — it does not even read identically
 *      to the same user on two machines.
 *   2. There is a second implementation. The API renders the same amounts
 *      into PDFs, e-mails and filings (apps/api/src/api/i18n/formatting.py),
 *      and Python's locale support is not ICU's. Two runtimes agreeing by
 *      coincidence is not agreement; ../formatting-cases.json is what makes
 *      it checkable, and it can only be checked against something both sides
 *      can implement exactly.
 *   3. `Intl.NumberFormat.format` takes a `number`. Handing it one means the
 *      amount has been through a float — which is what CLAUDE.md rule four
 *      forbids, and which silently loses digits above 2^53, four digits
 *      inside `numeric(19,2)`'s range. `format` does accept a string in
 *      recent engines, but designing the money path around a method whose
 *      obvious overload is lossy is how the lossy one gets used.
 *
 * The cost is that this table has to be maintained by hand. It is one entry.
 *
 * --- Adding a jurisdiction (FR-LOC-005) ---
 *
 * Adding `nl-BE` or `de-DE` is adding a `LocaleSpec` to `LOCALES` below, rows
 * to ../formatting-cases.json, and a value to the `formatting_locale` CHECK in
 * the migration. No branch in format.ts changes, which is what "without
 * forking the codebase" has to mean at this layer.
 */

/**
 * The administration locales LEDGR supports. One today: LEDGR serves Dutch
 * SMBs, and a second entry would be a guess about a jurisdiction nobody has
 * specified a chart of accounts, a VAT ruleset or a filing channel for.
 *
 * Mirrored by the `administration_formatting_locale` CHECK in migration 0030.
 */
export const FORMATTING_LOCALES = ["nl-NL"] as const;

export type FormattingLocale = (typeof FORMATTING_LOCALES)[number];

/**
 * The locale every administration starts in, and the one a caller falls back
 * to when an administration's locale is not to hand.
 */
export const DEFAULT_FORMATTING_LOCALE: FormattingLocale = "nl-NL";

export function isFormattingLocale(value: unknown): value is FormattingLocale {
  return typeof value === "string" && (FORMATTING_LOCALES as readonly string[]).includes(value);
}

/**
 * Everything format.ts needs, stated rather than derived. A date pattern is a
 * template over four tokens (`yyyy`, `MM`, `dd`, `d`, `MMMM`) instead of a
 * hard-coded ordering, so a locale that writes the year first is a string
 * here rather than a branch there.
 */
export interface LocaleSpec {
  readonly code: FormattingLocale;
  readonly decimalSeparator: string;
  readonly groupSeparator: string;
  readonly groupSize: number;
  readonly currencySymbol: string;
  /**
   * Between symbol and digits. U+00A0, so `€` and `1.234,56` cannot be split
   * across two lines of an invoice — a break there reads as two figures.
   */
  readonly currencySpace: string;
  /** True when the symbol precedes the amount, as `€ 1.234,56` does. */
  readonly currencySymbolFirst: boolean;
  /** Fixed-width, for columns that are meant to be scanned. */
  readonly shortDatePattern: string;
  /** Prose, for a sentence or a document heading. */
  readonly longDatePattern: string;
  /**
   * January first. Locale data, not UI text — which is why these live here
   * and not in the message catalogue. A Dutch administration's dates read
   * "2 september 2026" to an English-language user, and that is FR-LOC-002
   * working, not a missing translation.
   */
  readonly monthNames: readonly [
    string,
    string,
    string,
    string,
    string,
    string,
    string,
    string,
    string,
    string,
    string,
    string,
  ];
}

// Written as an escape on purpose: a literal U+00A0 is invisible in a diff,
// and this one character is the difference between the cases in
// ../formatting-cases.json passing and failing.
const NBSP = "\u00a0";

const NL_NL: LocaleSpec = {
  code: "nl-NL",
  decimalSeparator: ",",
  groupSeparator: ".",
  groupSize: 3,
  currencySymbol: "€",
  currencySpace: NBSP,
  currencySymbolFirst: true,
  shortDatePattern: "dd-MM-yyyy",
  longDatePattern: "d MMMM yyyy",
  // Lower case. Dutch month names are not capitalised, standalone or in a
  // sentence; capitalising them is an English habit that makes a Dutch
  // invoice look machine-produced.
  monthNames: [
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
  ],
};

export const LOCALES: Readonly<Record<FormattingLocale, LocaleSpec>> = {
  "nl-NL": NL_NL,
};

export function localeSpec(locale: FormattingLocale): LocaleSpec {
  return LOCALES[locale];
}
