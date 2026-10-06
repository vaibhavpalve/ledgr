/** The product name is a proper noun, the same in both languages. */
const WORD = "Boeklite";

/**
 * The balanced B (design system, Logos): a stem and two equal bowls split by a
 * hairline, debit equal to credit. The lockup is the glyph at cap height, then
 * "Boeklite" as live type in Instrument Serif. `onPanel` is the one-colour
 * version for a `pistachio` ground, where the two-colour glyph is not allowed.
 * Colours come from the `--glyph-*` tokens, so the theme flips them.
 */
export function Logo({ size = 28, onPanel }: { size?: number; onPanel?: boolean }) {
  return (
    <span className={`ui-logo${onPanel ? " ui-logo--on-pistachio" : ""}`}>
      <svg
        className="ui-logo__glyph"
        height={size}
        width={(size * 36) / 48}
        viewBox="0 0 36 48"
        aria-hidden="true"
      >
        <rect className="a" x="0" y="0" width="11" height="48" rx="2" />
        <path className="a" d="M13 0h7a11.5 11.5 0 0 1 0 23h-7z" />
        <path className="b" d="M13 25h7a11.5 11.5 0 0 1 0 23h-7z" />
      </svg>
      <span className="ui-logo__word">{WORD}</span>
    </span>
  );
}
