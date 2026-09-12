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
 */
export function Wordmark() {
  return (
    <span className="app__wordmark" data-testid="wordmark">
      LEDGR
    </span>
  );
}
