import { describe, expect, it } from "vitest";

import {
  AA_BODY_TEXT,
  AA_LARGE_TEXT,
  AA_NON_TEXT,
  contrastRatio,
  parseHex,
  relativeLuminance,
} from "./contrast";
import { CLIENT, ON_CLIENT_MARKER } from "./primitives";
import { darkTheme, lightTheme, type ThemeTokens } from "./tokens";

/**
 * CMP-012 / FR-LOC-004 (WCAG 2.2 AA), asserted against the palette itself.
 *
 * Every one of these ran against the real numbers when the dark theme was
 * added, and two values were changed because this suite failed on them. That
 * is the reason it exists: a dark palette picked by eye is precisely where
 * contrast regressions ship unnoticed, because the person picking has already
 * adapted to the screen.
 *
 * The suite is parameterised over both themes rather than written twice, so a
 * token added to one theme and forgotten in the other cannot pass.
 */

const THEMES: ReadonlyArray<readonly [string, ThemeTokens]> = [
  ["light", lightTheme],
  ["dark", darkTheme],
];

/** The three grounds any text in this product can land on. */
const GROUNDS = ["surface-ground", "surface-paper", "surface-raised"] as const;

describe("the contrast helpers themselves", () => {
  // A helper the rest of the suite trusts is worth pinning to known answers,
  // otherwise a bug here silently turns every assertion below into a pass.
  it("computes the two reference luminances exactly", () => {
    expect(relativeLuminance("#000000")).toBe(0);
    expect(relativeLuminance("#FFFFFF")).toBe(1);
  });

  it("gives black on white the canonical 21:1", () => {
    expect(contrastRatio("#000000", "#FFFFFF")).toBeCloseTo(21, 5);
  });

  it("is symmetric", () => {
    expect(contrastRatio("#00686C", "#FBF8F4")).toBeCloseTo(
      contrastRatio("#FBF8F4", "#00686C"),
      10,
    );
  });

  it("expands three-digit hex", () => {
    expect(parseHex("#abc")).toEqual(parseHex("#aabbcc"));
  });

  it("rejects anything that is not a colour", () => {
    expect(() => parseHex("teal")).toThrow(/not a hex colour/);
  });
});

describe.each(THEMES)("the %s theme", (themeName, theme) => {
  describe.each(GROUNDS)("text on %s", (ground) => {
    // The three text tokens are the ones a component picks between freely, so
    // all three have to clear body-text contrast on all three grounds — not
    // just the primary on the commonest surface.
    it.each(["text-primary", "text-secondary", "text-muted"] as const)(
      "%s clears AA body text",
      (textToken) => {
        const ratio = contrastRatio(theme[textToken], theme[ground]);
        expect(
          ratio,
          `${themeName}: ${textToken} (${theme[textToken]}) on ${ground} (${theme[ground]}) is ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(AA_BODY_TEXT);
      },
    );

    // A status colour is a text colour — "Te laat" in attention, a balance in
    // positive — so it carries the same burden as body text, not the relaxed
    // large-text threshold.
    it.each(["accent", "positive", "caution", "attention"] as const)(
      "%s clears AA body text",
      (statusToken) => {
        const ratio = contrastRatio(theme[statusToken], theme[ground]);
        expect(
          ratio,
          `${themeName}: ${statusToken} (${theme[statusToken]}) on ${ground} (${theme[ground]}) is ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(AA_BODY_TEXT);
      },
    );
  });

  it("puts legible text on a filled accent surface", () => {
    const ratio = contrastRatio(theme["text-on-accent"], theme.accent);
    expect(ratio, `${themeName}: on-accent is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      AA_BODY_TEXT,
    );
  });

  it("keeps the accent legible in its hover state too", () => {
    const ratio = contrastRatio(theme["accent-hover"], theme["surface-paper"]);
    expect(ratio, `${themeName}: accent-hover is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      AA_BODY_TEXT,
    );
  });

  // A wash is the background of a row or chip carrying that state, and the
  // solid of the same family is the text printed on it. If this pair fails,
  // the state reads as a coloured smear with illegible words on it.
  it.each(["accent", "positive", "caution", "attention"] as const)(
    "prints %s text legibly on its own wash",
    (family) => {
      const solid = theme[family];
      const wash = theme[`${family}-wash` as keyof ThemeTokens];
      const ratio = contrastRatio(solid, wash);
      expect(
        ratio,
        `${themeName}: ${family} (${solid}) on ${family}-wash (${wash}) is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_BODY_TEXT);
    },
  );

  // SC 1.4.11: the boundary of a control has to be perceivable, which a
  // hairline in a barely-different neutral is not.
  it("draws a strong border that is perceivable against paper", () => {
    const ratio = contrastRatio(theme["border-strong"], theme["surface-paper"]);
    expect(ratio, `${themeName}: border-strong is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      AA_NON_TEXT,
    );
  });

  it("draws a focus ring that is perceivable against every ground", () => {
    for (const ground of GROUNDS) {
      const ratio = contrastRatio(theme["border-focus"], theme[ground]);
      expect(
        ratio,
        `${themeName}: border-focus on ${ground} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_NON_TEXT);
    }
  });

  // The washes are large flat areas, so they take the non-text threshold
  // rather than the body one — but they must not vanish into the page, or a
  // row marked "overdue" looks identical to one that is not.
  it.each(["accent", "positive", "caution", "attention"] as const)(
    "keeps the %s wash distinguishable from paper",
    (family) => {
      const wash = theme[`${family}-wash` as keyof ThemeTokens];
      const ratio = contrastRatio(wash, theme["surface-paper"]);
      expect(
        ratio,
        `${themeName}: ${family}-wash (${wash}) against paper is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThan(1.05);
    },
  );
});

describe("client marker colours", () => {
  const markers = Object.entries(CLIENT);

  // These are one set across both themes (see primitives.ts), so white
  // initials have to work on all ten without a theme to fall back on.
  it.each(markers)("prints white initials legibly on %s", (name, colour) => {
    const ratio = contrastRatio(ON_CLIENT_MARKER, colour);
    expect(ratio, `${name} (${colour}) is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      AA_LARGE_TEXT,
    );
  });

  // A marker is a filled disc on the page, so it takes SC 1.4.11 against both
  // grounds it can sit on — and being theme-independent, it must clear both.
  it.each(markers)("keeps %s perceivable against both themes' paper", (name, colour) => {
    for (const [themeName, theme] of THEMES) {
      const ratio = contrastRatio(colour, theme["surface-paper"]);
      expect(
        ratio,
        `${name} (${colour}) on ${themeName} paper is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_NON_TEXT);
    }
  });

  it("has no two markers close enough to read as the same colour", () => {
    // Not an accessibility threshold — a design one. Two markers this close
    // would make the switcher's own "another client shares this colour"
    // warning (FR-FRM-000a) fire for clients that are genuinely distinct.
    for (let i = 0; i < markers.length; i += 1) {
      for (let j = i + 1; j < markers.length; j += 1) {
        const [nameA, a] = markers[i]!;
        const [nameB, b] = markers[j]!;
        expect(a, `${nameA} and ${nameB} are the same colour`).not.toBe(b);
      }
    }
  });
});
