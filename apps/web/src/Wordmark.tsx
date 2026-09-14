/**
 * The product's name, in the one place it is written.
 *
 * It reads LEDGR in Dutch and in English, so it is the single literal in this
 * app allowed past `scripts/check_translations.py` — by a named entry in that
 * script's LITERAL_ALLOWLIST rather than by the check not looking.
 *
 * A component rather than the string repeated in each header, so the allowlist
 * has one entry instead of one per screen. A list that grows every time the
 * wordmark appears somewhere new stops being read, and an allowlist nobody
 * reads is where an untranslated sentence eventually hides.
 *
 * --- The mark (ADR-055) ---
 *
 * Two entries ruled off by a double line: the bookkeeper's own notation for a
 * figure that is final and will not be reopened.
 *
 * The previous mark was an axis with three ascending bars — a chart glyph a
 * thousand products use, and one that describes REPORTING. This product's
 * distinguishing property is that the ledger is append-only (FR-GL-003): a
 * posted entry is never altered, only reversed. The double rule is the oldest
 * and most precise way of saying that, and it belongs to accounting rather
 * than to software.
 */
export function Wordmark() {
  return (
    <span className="wordmark" data-testid="wordmark">
      <svg
        className="wordmark__mark"
        width="22"
        height="22"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        aria-hidden="true"
      >
        {/* Two entries, the second shorter — a column that is still being kept. */}
        <path d="M4 5.5h12M4 9.5h7.5" />
        {/* Ruled off. Thinner, because the rule is notation, not content. */}
        <path d="M4 14h12M4 16.5h12" strokeWidth="1.2" />
      </svg>
      LEDGR
    </span>
  );
}
