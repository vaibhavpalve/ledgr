import { Logo } from "./ui";

/**
 * The product's name, in the one place it is written.
 *
 * It reads Boeklite in Dutch and in English, so it is the single literal in this
 * app allowed past `scripts/check_translations.py` — by a named entry in that
 * script's LITERAL_ALLOWLIST rather than by the check not looking.
 *
 * A component rather than the string repeated in each header, so the allowlist
 * has one entry instead of one per screen. A list that grows every time the
 * wordmark appears somewhere new stops being read, and an allowlist nobody
 * reads is where an untranslated sentence eventually hides.
 *
 * --- The mark (ADR-104) ---
 *
 * The balanced B of the Boeklite design system, drawn by ui/Logo. This component stays as the
 * one named place the name is written for the screens outside the shell.
 */
export function Wordmark() {
  return (
    <span className="wordmark" data-testid="wordmark">
      <Logo />
    </span>
  );
}
