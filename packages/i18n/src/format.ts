/**
 * Rendering money, numbers and dates into an administration's locale —
 * FR-LOC-002, and CLAUDE.md rule four.
 *
 * Every case this file is expected to get right lives in
 * ../formatting-cases.json, which apps/api/tests/i18n/test_formatting.py runs
 * against the Python implementation of the same rules. Neither side is
 * allowed to be right on its own.
 *
 * --- Money never becomes a number ---
 *
 *   "No financial calculation uses floating point. Monetary values use
 *    decimal types with defined scale, everywhere in the calculation path."
 *
 * Formatting is the end of that path, and it is the easiest place to lose the
 * guarantee: `Number("12345678901234567.89")` is already wrong, and it looks
 * fine. So the functions here take a DECIMAL STRING — the shape the API sends
 * and Postgres's `numeric` produces — and take it apart with string
 * operations. No `parseFloat`, no `Number`, no arithmetic on the value at
 * all; the only number in this file is a digit count.
 *
 * TypeScript enforces the same thing at the call site: `formatMoney` accepts
 * `string`, so passing a `number` is a compile error rather than a rounding
 * error. That mirrors the tripwire api.ledger.model puts at its own boundary
 * — a float that reaches here has already lost the digits, and accepting it
 * would print a wrong number that everything downstream agrees with.
 *
 * --- Refusing rather than coping ---
 *
 * Every function here throws on input it cannot render exactly. That is a
 * deliberate choice against the friendlier alternative of rounding, trimming
 * or returning an empty string, because each of those turns a bug into a
 * plausible-looking figure on an invoice. A thrown error is seen; `€ 0,00`
 * where `€ 1.234,57` belonged is not.
 */

import type { Language } from "./language";
import { pluralCategory } from "./language";
import type { FormattingLocale, LocaleSpec } from "./locale";
import { localeSpec } from "./locale";

/**
 * The scale of a posted amount, matching `numeric(19,2)` in migration 0020
 * and `SCALE` in api.ledger.model. FR-GL-010's multi-currency work adds
 * amounts in other currencies beside the functional ones; it does not make
 * the euro's scale variable.
 */
export const MONEY_SCALE = 2;

export class FormattingError extends Error {}

interface ParsedDecimal {
  negative: boolean;
  /** Digits before the point, at least one, no separators. */
  integerDigits: string;
  /** Digits after the point; empty when the input had no point. */
  fractionDigits: string;
}

/**
 * Strictly `-?digits[.digits]`, and nothing else.
 *
 * Everything this rejects is a way a wrong figure reaches a screen looking
 * right, and ../formatting-cases.json spells out each one:
 *
 *   `1,234.56` / `1.234,56`   already formatted, in this locale or another.
 *                             Re-parsing either silently reinterprets the
 *                             separators, and a round trip through the UI is
 *                             how one arrives.
 *   `1e3`                     exponent notation, which is what a float
 *                             stringifies to. Accepting it accepts whatever
 *                             produced it.
 *   `+1.00`, ` 1.00 `         a foreign format or a padded field. Coping
 *                             quietly is how the next one goes unnoticed.
 *   `1234.`, `.56`, ``        not decimal literals.
 */
function parseDecimal(value: string): ParsedDecimal {
  if (typeof value !== "string" || !/^-?\d+(\.\d+)?$/.test(value)) {
    throw new FormattingError(
      `not an exact decimal: ${JSON.stringify(value)}. Amounts cross the wire as ` +
        `decimal strings (\`"1234.56"\`), never as JSON numbers — a JSON number is ` +
        `parsed into a float before this function can see it (NFR-031).`,
    );
  }

  const negative = value.startsWith("-");
  const unsigned = negative ? value.slice(1) : value;
  const [integerDigits = "", fractionDigits = ""] = unsigned.split(".");
  return { negative, integerDigits, fractionDigits };
}

/** `1234567` → `1.234.567`, from the right, so the first group may be short. */
function group(digits: string, spec: LocaleSpec): string {
  const parts: string[] = [];
  for (let end = digits.length; end > 0; end -= spec.groupSize) {
    parts.unshift(digits.slice(Math.max(0, end - spec.groupSize), end));
  }
  return parts.join(spec.groupSeparator);
}

/**
 * The digits and separators, with no sign and no symbol: `1.234,56`.
 * `scale` is exact, not a maximum — a value carrying more precision than the
 * caller asked to display is refused rather than rounded, because rounding
 * here prints a number that is not the number in the ledger.
 */
function renderDigits(parsed: ParsedDecimal, scale: number, spec: LocaleSpec): string {
  if (parsed.fractionDigits.length > scale) {
    throw new FormattingError(
      `${parsed.integerDigits}.${parsed.fractionDigits} carries ` +
        `${parsed.fractionDigits.length} decimal places and is being rendered at ` +
        `scale ${scale}. Rounding it here would display a figure that differs from ` +
        `the stored one; round deliberately, upstream, or render at its own scale.`,
    );
  }

  const whole = group(parsed.integerDigits, spec);
  if (scale === 0) return whole;
  return whole + spec.decimalSeparator + parsed.fractionDigits.padEnd(scale, "0");
}

/** True when every digit is zero, so `-0.00` does not render a minus sign. */
function isZero(parsed: ParsedDecimal): boolean {
  return /^0*$/.test(parsed.integerDigits + parsed.fractionDigits);
}

/**
 * `"1234.56"` → `"€ 1.234,56"`.
 *
 * The sign sits between the symbol and the digits (`€ -1.234,56`), which is
 * CLDR nl-NL's currency pattern `¤ #,##0.00;¤ -#,##0.00`. Writing it the
 * English way round produces `-€ 1.234,56`, which a Dutch reader notices.
 */
export function formatMoney(amount: string, locale: FormattingLocale): string {
  const spec = localeSpec(locale);
  const parsed = parseDecimal(amount);
  const digits = renderDigits(parsed, MONEY_SCALE, spec);
  const signed = (parsed.negative && !isZero(parsed) ? "-" : "") + digits;

  return spec.currencySymbolFirst
    ? spec.currencySymbol + spec.currencySpace + signed
    : signed + spec.currencySpace + spec.currencySymbol;
}

/**
 * A bare number: a quantity, a rate, a count of lines. Unlike money, the sign
 * sits directly before the digits, because there is no symbol for it to
 * follow.
 *
 * `scale` has no default. A caller that has not decided how many decimal
 * places its value has is a caller about to display a rate as `21,00` or an
 * amount as `1.234,5`, and asking is cheaper than guessing.
 */
export function formatNumber(
  value: string,
  locale: FormattingLocale,
  options: { scale: number },
): string {
  const spec = localeSpec(locale);
  const parsed = parseDecimal(value);
  const digits = renderDigits(parsed, options.scale, spec);
  return (parsed.negative && !isZero(parsed) ? "-" : "") + digits;
}

export type DateStyle = "short" | "long" | "iso";

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * `"2026-09-02"` → `"02-09-2026"` (short) or `"2 september 2026"` (long).
 *
 * Takes an ISO date STRING, not a `Date`. A `Date` is an instant, and an
 * instant rendered in the reader's timezone is how a posting dated 1 January
 * appears in December: the date on a journal entry, an invoice or a VAT
 * return is a calendar date in the administration's own reckoning and has no
 * timezone to be moved by. `"2026-09-02T00:00:00Z"` is refused for that
 * reason and not as pedantry.
 *
 * `iso` returns the input unchanged, and exists so that machine-facing output
 * has a named style rather than being the one place a caller skips this
 * function. The argument is `ledger.money_text`'s (migration 0023): a value a
 * consumer parses must not depend on anybody's locale.
 */
export function formatDate(
  isoDate: string,
  locale: FormattingLocale,
  style: DateStyle = "short",
): string {
  const spec = localeSpec(locale);
  const match = typeof isoDate === "string" ? ISO_DATE.exec(isoDate) : null;
  if (match === null) {
    throw new FormattingError(
      `not an ISO 8601 calendar date: ${JSON.stringify(isoDate)}. Expected ` +
        `YYYY-MM-DD — zero-padded, no time, no offset.`,
    );
  }

  const [, yearText = "", monthText = "", dayText = ""] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);

  // Rejects month 13 and 30 February. A lenient parser rolls both forward,
  // which moves a posting into the next period — silently, and by exactly the
  // amount that makes a period's totals wrong rather than obviously broken.
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) {
    throw new FormattingError(`no such calendar date: ${isoDate}`);
  }

  if (style === "iso") return isoDate;

  const pattern = style === "long" ? spec.longDatePattern : spec.shortDatePattern;
  return renderDatePattern(pattern, {
    yyyy: yearText,
    MM: monthText,
    dd: dayText,
    d: String(day),
    MMMM: spec.monthNames[month - 1] ?? "",
  });
}

function daysInMonth(year: number, month: number): number {
  // Day 0 of the next month is the last day of this one, which handles leap
  // years without restating the rule.
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

/**
 * Longest token first, so `MMMM` is not consumed as `MM` followed by a
 * literal `MM`, and `dd` is not consumed as two `d`s.
 */
const DATE_TOKENS = ["yyyy", "MMMM", "MM", "dd", "d"] as const;

function renderDatePattern(pattern: string, values: Record<string, string>): string {
  let out = "";
  let index = 0;
  outer: while (index < pattern.length) {
    for (const token of DATE_TOKENS) {
      if (pattern.startsWith(token, index)) {
        out += values[token] ?? "";
        index += token.length;
        continue outer;
      }
    }
    out += pattern[index] ?? "";
    index += 1;
  }
  return out;
}

/**
 * Re-exported so a caller formatting a counted message reaches for one import
 * rather than two. The category depends on the reader's LANGUAGE — grammar is
 * a property of the words — while everything else in this file depends on the
 * administration's LOCALE. That split is FR-LOC-002 in one file.
 */
export { pluralCategory };
export type { Language };
