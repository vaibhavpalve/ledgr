/** The product name is a proper noun, the same in both languages. */
const WORD = "Boeklite";

/**
 * The balanced B (design system, Logos): a stem and two equal bowls split by a
 * hairline, debit equal to credit. The lockup is the glyph at 1.05x the type
 * size, a 10px gap, then "Boeklite" as live type in Inter display 600 (ADR-106).
 * `size` is the word's font size. `onForest` is the reversed version for the
 * `forest` ground: light stem and upper bowl, lemon lower bowl. Elsewhere the
 * colours come from the `--glyph-*` tokens, so the theme flips them.
 */
export function Logo({ size = 20, onForest }: { size?: number; onForest?: boolean }) {
  return (
    <span className={`ui-logo${onForest ? " ui-logo--on-forest" : ""}`} style={{ fontSize: size }}>
      <svg className="ui-logo__glyph" viewBox="0 0 36 48" aria-hidden="true">
        <rect className="a" x="0" y="0" width="11" height="48" rx="2" />
        <path className="a" d="M13 0h7a11.5 11.5 0 0 1 0 23h-7z" />
        <path className="b" d="M13 25h7a11.5 11.5 0 0 1 0 23h-7z" />
      </svg>
      <span className="ui-logo__word">{WORD}</span>
    </span>
  );
}
