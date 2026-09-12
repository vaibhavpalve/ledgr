import { useEffect } from "react";
import type { Language } from "@ledgr/i18n";

/**
 * Keeps `<html lang>` on the language actually being rendered — WCAG 2.2
 * success criterion 3.1.1 (Language of Page), which FR-LOC-004 commits to.
 *
 * --- Why this is not cosmetic ---
 *
 * `<html lang>` is what a screen reader chooses its pronunciation rules and
 * voice from. Left at `en` while the page renders Dutch, "Geen klant
 * geselecteerd" is read letter-mangled by an English synthesiser, and
 * "Kantoorbeheerder" is unintelligible. It also drives hyphenation, the
 * spellchecker in every text field a bookkeeper types a description into, and
 * what a translation tool offers to do to the page.
 *
 * So it is part of "taking effect immediately" (FR-LOC-001a) rather than a
 * detail beside it: a language switch that repaints the words and leaves the
 * page still declaring itself English has changed the product for sighted
 * users only.
 *
 * --- Why it lives in the web app rather than in @ledgr/i18n ---
 *
 * `I18nProvider` is shared with React Native (MOB-016), where there is no
 * document and no `<html>`. Guarding a DOM write inside the shared provider
 * would work and would put a browser concern in the one module that is
 * meant to have none. `device.ts` is the package's browser adapter and this
 * is the app's.
 */
export function useDocumentLanguage(language: Language): void {
  useEffect(() => {
    // Guarded rather than assumed: this also runs under a non-DOM test
    // environment, and a crash here would take down the whole tree over an
    // attribute.
    if (typeof document === "undefined") return;
    document.documentElement.lang = language;
  }, [language]);
}
