/**
 * Display helpers for the two invoice-line fields the API stores at four
 * decimals — `quantity` and `unit_price` are `numeric(19,4)` (migration 0037),
 * so they cross the wire as `"100.0000"`.
 *
 * `money()` renders at exactly two decimals and refuses a longer string rather
 * than round it (NFR-031), so `money("100.0000")` throws. On the detail screen
 * that throw happened during render and blanked the whole page. These strip
 * TRAILING ZEROS only — the value never changes, nothing is rounded — down to
 * the scale the caller wants as a minimum, so `"100.0000"` becomes `"100.00"`
 * while a genuine `"99.9950"` keeps its four places.
 */

/** Currency symbol and amount are kept apart by a non-breaking space, as money() does. */
const NBSP = String.fromCharCode(0xa0);

export interface TrimmedDecimal {
  readonly text: string;
  readonly scale: number;
}

/** Drops trailing zeros of the fraction, but never below `minScale` places. */
export function trimDecimal(value: string, minScale: number): TrimmedDecimal {
  const [whole = "", fraction = ""] = value.split(".");
  let end = fraction.length;
  while (end > minScale && fraction[end - 1] === "0") end -= 1;
  const kept = fraction.slice(0, end).padEnd(minScale, "0");
  return { text: kept === "" ? whole : `${whole}.${kept}`, scale: kept.length };
}

interface Formatters {
  money(amount: string): string;
  number(value: string, options: { scale: number }): string;
}

/** A unit price: `€ 100,00` normally, `€ 99,9950` when it really has four places. */
export function formatUnitPrice(value: string, { money, number }: Formatters): string {
  const { text, scale } = trimDecimal(value, 2);
  return scale === 2 ? money(text) : `€${NBSP}${number(text, { scale })}`;
}

/** A quantity: `1`, `2,5`, `0,125` - no more places than it needs. */
export function formatQuantity(value: string, { number }: Pick<Formatters, "number">): string {
  const { text, scale } = trimDecimal(value, 0);
  return number(text, { scale });
}
