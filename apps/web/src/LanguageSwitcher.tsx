import { SUPPORTED_LANGUAGES, translate, useI18n, type Language } from "@ledgr/i18n";

/**
 * FR-LOC-001a: "Language is selectable before login (IAM-010g) and changeable
 * at any time from the user menu in one click, taking effect immediately
 * without reload or re-authentication."
 *
 * --- Why buttons and not a select ---
 *
 * "One click" is the requirement, and a `<select>` is two: open it, then pick.
 * With exactly two languages a segmented control is the whole list, always
 * visible, one click to the other one — and it shows the current choice
 * without being opened, which a collapsed select does not.
 *
 * --- Why each language is named in its own language ---
 *
 * `Nederlands` and `English`, never `Dutch` and `Engels`. Someone who cannot
 * read the language currently on screen has to be able to find their own, and
 * the endonym is the one string they are certain to recognise. It is the
 * reason common.language.name.* are marked `identical` in the catalogue
 * rather than translated.
 *
 * --- Why it calls translate() directly for the labels ---
 *
 * Every other label here comes from `t()` and is therefore in the reader's
 * current language. The two option labels must not be: the label on the
 * English button reads `English` whichever language the UI is in. So they are
 * translated against the language they REPRESENT, not the active one — which
 * for these two records is the same string either way, and would stop being so
 * the moment a third language is added.
 *
 * --- Accessibility (FR-LOC-004, WCAG 2.2 AA) ---
 *
 * A `group` with an accessible name, and `aria-pressed` on each button so the
 * current choice is announced rather than only coloured. The control is
 * reachable and operable before authentication, where it has to work for
 * somebody who cannot read the page it is on.
 */
export function LanguageSwitcher({ compact = false }: { compact?: boolean } = {}) {
  const { language, setLanguage, t } = useI18n();
  const name = (option: Language) => translate(`common.language.name.${option}`, option);

  return (
    <div
      className="language-switcher"
      role="group"
      aria-label={t("common.language.choose")}
      data-testid="language-switcher"
    >
      {SUPPORTED_LANGUAGES.map((option: Language) => (
        <button
          key={option}
          type="button"
          lang={option}
          // In the compact form the visible text is the code (EN | NL), so the
          // full endonym is carried as the accessible name; it contains the
          // visible text's language, which is what WCAG 2.5.3 asks for.
          aria-label={compact ? name(option) : undefined}
          // Not `disabled` when active: a disabled control drops out of the
          // tab order, so a keyboard user tabbing through would find only the
          // language they are not using.
          aria-pressed={option === language}
          data-testid={`language-option-${option}`}
          onClick={() => setLanguage(option)}
        >
          {compact ? option.toUpperCase() : name(option)}
        </button>
      ))}
    </div>
  );
}
