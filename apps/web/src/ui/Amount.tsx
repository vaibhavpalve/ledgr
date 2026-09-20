import { useI18n } from "@ledgr/i18n";

const MINUS = "−";

/**
 * Formats a decimal STRING as money with the real minus sign (U+2212).
 *
 * Uses the app's exact-decimal formatter (`money` from @ledgr/i18n) rather
 * than a Number-backed Intl call, because NFR-031 forbids floating point on any
 * money path and the amount is a string the API sent. For nl-NL the output is
 * the same as `Intl.NumberFormat('nl-NL', {style:'currency', currency:'EUR'})`
 * except that a negative reads with U+2212 instead of a hyphen.
 */
export function useMoney(): (amount: string) => string {
  const { money } = useI18n();
  return (amount) => money(amount).replace("-", MINUS);
}

/**
 * An amount in the mono figure style (section 5, Amount). Right-align it in
 * tables with the `ui-num` cell class. `overdrawn` is the only way it turns
 * red and the caller must pair it with a word; debits are never `overdrawn`.
 */
export function Amount({
  value,
  overdrawn,
  className,
}: {
  value: string;
  overdrawn?: boolean;
  className?: string;
}) {
  const format = useMoney();
  return (
    <span
      className={`ui-amount${overdrawn ? " ui-amount--overdrawn" : ""}${className ? ` ${className}` : ""}`}
    >
      {format(value)}
    </span>
  );
}
