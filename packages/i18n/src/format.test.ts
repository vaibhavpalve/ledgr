/**
 * The TypeScript half of FR-LOC-002's cross-implementation check.
 *
 * Every case comes from ../formatting-cases.json, which
 * apps/api/tests/i18n/test_formatting.py runs against the Python
 * implementation. A case added here is therefore a case the API must satisfy
 * too, and a divergence between an amount on screen and the same amount on
 * the PDF of it fails both suites rather than neither.
 */

import { describe, expect, it } from "vitest";

import cases from "../formatting-cases.json";
import { FormattingError, formatDate, formatMoney, formatNumber } from "./format";
import { pluralCategory } from "./language";
import type { Language } from "./language";
import type { DateStyle } from "./format";
import type { FormattingLocale } from "./locale";

interface MoneyCase {
  locale: string;
  amount: string;
  expected: string;
}
interface NumberCase {
  locale: string;
  value: string;
  scale: number;
  expected: string;
}
interface DateCase {
  locale: string;
  date: string;
  style: string;
  expected: string;
}
interface PluralCase {
  language: string;
  count: number;
  expected: string;
}
interface RejectedCase {
  value: string;
  why: string;
}

const money = cases.money as MoneyCase[];
const numbers = cases.number as NumberCase[];
const dates = cases.date as DateCase[];
const plurals = cases.plural as PluralCase[];
const rejectedMoney = cases.rejected.money as RejectedCase[];
const rejectedDates = cases.rejected.date as RejectedCase[];

describe("the shared case table", () => {
  // The idiom tests/test_audit_coverage.py uses for its own route sweep: a
  // table-driven suite that silently finds no rows passes completely and
  // guarantees nothing, which is worse than having no suite because it reads
  // as a guarantee.
  it("actually has cases to run", () => {
    expect(money.length).toBeGreaterThan(0);
    expect(numbers.length).toBeGreaterThan(0);
    expect(dates.length).toBeGreaterThan(0);
    expect(plurals.length).toBeGreaterThan(0);
    expect(rejectedMoney.length).toBeGreaterThan(0);
    expect(rejectedDates.length).toBeGreaterThan(0);
  });
});

describe("formatMoney", () => {
  for (const testCase of money) {
    it(`${testCase.locale}: ${testCase.amount} → ${testCase.expected}`, () => {
      expect(formatMoney(testCase.amount, testCase.locale as FormattingLocale)).toBe(
        testCase.expected,
      );
    });
  }

  for (const testCase of rejectedMoney) {
    it(`refuses ${JSON.stringify(testCase.value)} — ${testCase.why}`, () => {
      expect(() => formatMoney(testCase.value, "nl-NL")).toThrow(FormattingError);
    });
  }

  it("never routes an amount through a float", () => {
    // 12345678901234567.89 is inside numeric(19,2) and outside the range a
    // double can represent exactly: Number() rounds it to ...568, four digits
    // from the end. Asserting the exact string here is what proves the
    // implementation works on digits rather than on a parsed value — the case
    // table would catch it too, and this says why it is in the table.
    const exact = "12345678901234567.89";
    expect(String(Number(exact))).not.toContain("34567.89");
    expect(formatMoney(exact, "nl-NL")).toBe("€ 12.345.678.901.234.567,89");
  });
});

describe("formatNumber", () => {
  for (const testCase of numbers) {
    it(`${testCase.locale}: ${testCase.value} @${testCase.scale} → ${testCase.expected}`, () => {
      expect(
        formatNumber(testCase.value, testCase.locale as FormattingLocale, {
          scale: testCase.scale,
        }),
      ).toBe(testCase.expected);
    });
  }

  it("refuses to round a value that carries more precision than the scale", () => {
    // The friendly alternative is to round to the requested scale. It is
    // rejected because 0,22 on screen where 0.215 is stored is a figure
    // somebody will reconcile against and fail to.
    expect(() => formatNumber("0.215", "nl-NL", { scale: 2 })).toThrow(FormattingError);
  });
});

describe("formatDate", () => {
  for (const testCase of dates) {
    it(`${testCase.locale}: ${testCase.date} (${testCase.style}) → ${testCase.expected}`, () => {
      expect(
        formatDate(testCase.date, testCase.locale as FormattingLocale, testCase.style as DateStyle),
      ).toBe(testCase.expected);
    });
  }

  for (const testCase of rejectedDates) {
    it(`refuses ${JSON.stringify(testCase.value)} — ${testCase.why}`, () => {
      expect(() => formatDate(testCase.value, "nl-NL", "short")).toThrow(FormattingError);
    });
  }

  it("refuses an impossible day rather than rolling it into the next month", () => {
    // The behaviour worth pinning: `new Date("2026-02-30")` in a lenient
    // parser is 2 March, which moves a posting into the next period without
    // saying so.
    expect(() => formatDate("2026-02-30", "nl-NL")).toThrow(FormattingError);
    expect(formatDate("2024-02-29", "nl-NL")).toBe("29-02-2024");
  });
});

describe("pluralCategory", () => {
  for (const testCase of plurals) {
    it(`${testCase.language}: ${testCase.count} → ${testCase.expected}`, () => {
      expect(pluralCategory(testCase.language as Language, testCase.count)).toBe(testCase.expected);
    });
  }

  it("gives a non-integer `other` in both languages, per CLDR", () => {
    // "1,5 regel" and "1.5 line" are both wrong; both languages want `other`
    // for anything that is not one. A `count <= 1` check — the tempting
    // shorthand, since it also handles zero — gets 0,5 wrong in both.
    expect(pluralCategory("nl", 1)).toBe("one");
    expect(pluralCategory("nl", 1.5)).toBe("other");
    expect(pluralCategory("en", 1.5)).toBe("other");
    expect(pluralCategory("en", 0.5)).toBe("other");
  });
});

describe("FR-LOC-002: formatting follows the administration, not the reader", () => {
  it("writes the same characters whatever language the reader is in", () => {
    // The requirement in one assertion. `formatMoney` takes a
    // FormattingLocale and there is no parameter for a language, so a caller
    // CANNOT make an amount depend on who is reading it — the property is
    // enforced by the signature rather than by everyone remembering it.
    //
    // The failure this prevents is an English-speaking owner reading
    // "1,234.56" off a screen a Dutch bookkeeper reads as "1.234,56", and the
    // two of them agreeing on the phone that the number matches.
    const amount = "1234.56";
    expect(formatMoney(amount, "nl-NL")).toBe("€ 1.234,56");
    expect(formatDate("2026-09-02", "nl-NL", "long")).toBe("2 september 2026");
  });
});
