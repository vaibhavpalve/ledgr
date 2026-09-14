/**
 * WCAG 2.1 relative luminance and contrast ratio.
 *
 * This exists so `contrast.test.ts` can ASSERT the palette meets CMP-012 /
 * FR-LOC-004 (WCAG 2.2 AA) rather than a comment claiming it does. Both themes
 * are checked by the same code, which is the part that matters: a dark theme
 * added by eye is exactly where contrast regressions ship unnoticed.
 *
 * The formulae are from WCAG 2.1 §"relative luminance" and §"contrast ratio",
 * transcribed rather than approximated.
 */

/** `#RGB` or `#RRGGBB` → the three channels as 0–255 integers. */
export function parseHex(hex: string): [number, number, number] {
  const value = hex.trim().replace(/^#/, "");

  const expanded =
    value.length === 3
      ? value
          .split("")
          .map((c) => c + c)
          .join("")
      : value;

  if (!/^[0-9a-fA-F]{6}$/.test(expanded)) {
    throw new Error(`not a hex colour: ${hex}`);
  }

  return [
    Number.parseInt(expanded.slice(0, 2), 16),
    Number.parseInt(expanded.slice(2, 4), 16),
    Number.parseInt(expanded.slice(4, 6), 16),
  ];
}

/**
 * WCAG relative luminance, 0 (black) to 1 (white).
 *
 * The per-channel step is sRGB's transfer function — the reason a naive
 * average of the channels gives the wrong answer, and the reason two colours
 * that "look similar" can be far apart here.
 */
export function relativeLuminance(hex: string): number {
  const [r, g, b] = parseHex(hex);

  const channel = (raw: number): number => {
    const c = raw / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };

  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/**
 * Contrast ratio between two colours, 1:1 (identical) to 21:1 (black/white).
 * Order does not matter.
 *
 * AA wants 4.5:1 for body text, 3:1 for large text (>=24px, or >=18.66px bold)
 * and for the non-text boundaries of a control (SC 1.4.11).
 */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const lighter = Math.max(la, lb);
  const darker = Math.min(la, lb);
  return (lighter + 0.05) / (darker + 0.05);
}

/** WCAG AA thresholds, named so assertions read as the requirement. */
export const AA_BODY_TEXT = 4.5;
export const AA_LARGE_TEXT = 3;
export const AA_NON_TEXT = 3;
