import { useI18n } from "@ledgr/i18n";
import { THEME_PREFERENCES, type ThemePreference } from "@ledgr/design-tokens";

import { useTheme } from "./useTheme";

/**
 * The light/dark control (ADR-055).
 *
 * --- Three options, not a switch ---
 *
 * A two-state toggle cannot express the default. "System" is not the same
 * choice as picking whichever theme the device happens to be in right now: a
 * user on System follows their OS when it flips at sunset, one who chose Light
 * does not. A binary switch forces everybody into an explicit choice they did
 * not make and silently breaks that following behaviour, so this is a
 * three-option segmented control — the same shape as `LanguageSwitcher`, for
 * the same reason (the current choice is visible without opening anything).
 *
 * --- Accessibility (FR-LOC-004, WCAG 2.2 AA) ---
 *
 * A named `group` with `aria-pressed` on each option, matching
 * `LanguageSwitcher` rather than inventing a second pattern for the same job.
 * None of the three is ever `disabled`: a disabled control leaves the tab
 * order, so a keyboard user would be able to reach only the options they had
 * not chosen.
 *
 * The icons are `aria-hidden` — each button already carries its own text, and
 * an announced "sun, Light" is noise.
 */
export function ThemeToggle() {
  const { t } = useI18n();
  const { preference, resolved, setPreference } = useTheme();

  return (
    <div
      className="theme-toggle"
      role="group"
      aria-label={t("common.theme.label")}
      // The RESOLVED theme, exposed for tests and for any styling that needs
      // to know what is actually on screen rather than what was chosen.
      data-resolved={resolved}
      data-testid="theme-toggle"
    >
      {THEME_PREFERENCES.map((option: ThemePreference) => (
        <button
          key={option}
          type="button"
          className="theme-toggle__option"
          aria-pressed={option === preference}
          data-testid={`theme-option-${option}`}
          onClick={() => setPreference(option)}
        >
          <ThemeIcon preference={option} />
          <span>{t(`common.theme.${option}`)}</span>
        </button>
      ))}
    </div>
  );
}

/**
 * One 20-unit grid, round caps and joins, 1.5px painted stroke — the icon
 * contract the whole product follows, so these sit correctly beside every
 * other icon rather than reading half a weight heavier.
 */
function ThemeIcon({ preference }: { preference: ThemePreference }) {
  const shared = {
    width: 18,
    height: 18,
    viewBox: "0 0 20 20",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.67,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
  } as const;

  if (preference === "light") {
    return (
      <svg {...shared}>
        <circle cx="10" cy="10" r="3.4" />
        <path d="M10 2.6v2M10 15.4v2M17.4 10h-2M4.6 10h-2M15.2 4.8l-1.4 1.4M6.2 13.8l-1.4 1.4M15.2 15.2l-1.4-1.4M6.2 6.2 4.8 4.8" />
      </svg>
    );
  }

  if (preference === "dark") {
    return (
      <svg {...shared}>
        <path d="M16.2 11.8A6.8 6.8 0 0 1 8.2 3.8a6.8 6.8 0 1 0 8 8z" />
      </svg>
    );
  }

  // System: a display, because the choice is "whatever this device says".
  return (
    <svg {...shared}>
      <rect x="2.8" y="4" width="14.4" height="9.6" rx="1.4" />
      <path d="M7.4 17h5.2M10 13.6V17" />
    </svg>
  );
}
