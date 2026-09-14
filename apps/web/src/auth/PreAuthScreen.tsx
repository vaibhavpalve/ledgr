import type { ReactNode } from "react";
import { useI18n } from "@ledgr/i18n";

import "./PreAuthScreen.css";
import { LanguageSwitcher } from "../LanguageSwitcher";
import { ThemeToggle } from "../theme/ThemeToggle";
import { Wordmark } from "../Wordmark";

export type PreAuthScreenKind = "login" | "signup" | "mfa";

const HEADING_KEY: Record<PreAuthScreenKind, string> = {
  login: "auth.sign_in.heading",
  signup: "auth.sign_up.heading",
  // ADR-054: shown to an already-authenticated caller who has not yet
  // cleared IAM-011's MFA gate - "mfa" is not one of IAM-010g's two named
  // screens, but reuses this same frame (Wordmark, heading, the pre-auth
  // language control) rather than inventing a second one, since nothing
  // about that layout is specific to signing in or signing up.
  mfa: "auth.mfa.heading",
};

/**
 * The six things the marketing rail leads with (ADR-057) — each traceable to
 * a real, shipped requirement, not a claim invented for this screen. Order is
 * roughly "what a new SMB owner feels day to day" first, "what an accountant
 * checks" second: capture is the most frequent action in the product,
 * passkey sign-in is the least.
 */
const FEATURES: ReadonlyArray<{ icon: ReactNode; titleKey: string; bodyKey: string }> = [
  {
    titleKey: "auth.marketing.feature.capture_title",
    bodyKey: "auth.marketing.feature.capture_body",
    icon: (
      <path d="M3 7.5h2.8l1.4-2h5.6l1.4 2H17a1 1 0 0 1 1 1V15a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V8.5a1 1 0 0 1 1-1z M10 13.5a2.8 2.8 0 1 0 0-5.6 2.8 2.8 0 0 0 0 5.6z" />
    ),
  },
  {
    titleKey: "auth.marketing.feature.immutable_title",
    bodyKey: "auth.marketing.feature.immutable_body",
    // The wordmark's own glyph — two kept entries, ruled off. The clearest
    // way to say "permanent" is the mark this product already uses for it.
    icon: <path d="M4 5.5h12M4 9.5h7.5 M4 14h12M4 16.5h12" strokeWidth="1.4" />,
  },
  {
    titleKey: "auth.marketing.feature.switcher_title",
    bodyKey: "auth.marketing.feature.switcher_body",
    icon: <path d="M4 10h12M11.6 5.6 16 10l-4.4 4.4 M8.4 5.6 4 10l4.4 4.4" strokeWidth="1.4" />,
  },
  {
    titleKey: "auth.marketing.feature.precision_title",
    bodyKey: "auth.marketing.feature.precision_body",
    icon: <path d="M10 3.4v13.2 M6.6 6h5a2.2 2.2 0 0 1 0 4.4h-3.2a2.2 2.2 0 0 0 0 4.4h5.2" />,
  },
  {
    titleKey: "auth.marketing.feature.dashboard_title",
    bodyKey: "auth.marketing.feature.dashboard_body",
    icon: <path d="M10 10 17.3 6.6A7.3 7.3 0 1 0 17.3 13.4Z M10 10V2.7A7.3 7.3 0 0 1 17.3 6.6Z" />,
  },
  {
    titleKey: "auth.marketing.feature.passkey_title",
    bodyKey: "auth.marketing.feature.passkey_body",
    icon: (
      <path d="M8.2 11.8a3.6 3.6 0 1 1 2.9-1.4L16 15.3v1.9h-1.9v-1.4h-1.9v-1.9h-1.4l-1.3-1.3a3.6 3.6 0 0 1-1.3.2z" />
    ),
  },
];

/**
 * The frame the login, signup and MFA-enrolment screens render inside —
 * IAM-010g, FR-ONB-000, and (ADR-057) the marketing rail that used to be
 * missing entirely: a person's first encounter with LEDGR was a single bare
 * card, no different from a support-tool login. This is the product's own
 * front page as much as it is a door — the two references it was drawn from
 * (see ADR-057) are themselves nothing but a combined marketing-and-login
 * page, not a separate homepage.
 *
 *   IAM-010g   "Language is selectable BEFORE authentication, on the login and
 *              signup screens, defaulting to the browser or device locale and
 *              falling back to Dutch for `.nl` traffic. The choice persists on
 *              the device and is applied to the account after first login."
 *
 * --- Two regions, one DOM order that is NOT the visual order ---
 *
 * `.pre-auth__panel` (the actual task — heading, form, language control)
 * comes FIRST in markup; `.pre-auth__marketing` (the rail) comes second.
 * `app.css`'s grid then places the rail visually on the left on a wide
 * viewport. This is deliberate, not incidental: a screen-reader or keyboard
 * user reaches the thing they came to do — signing in — before a marketing
 * pitch, the same reasoning `pre-auth__language`'s placement already uses
 * for the language control. The rail is still real, readable content
 * (an `aside` landmark, not `aria-hidden`) — it is ordered after the task,
 * not hidden from anyone.
 *
 * --- Why the rail's copy does not change per screen ---
 *
 * Login, signup and MFA enrolment show the SAME rail. A pitch that changed
 * mid-flow (present it at signup, drop it at MFA) would read as the product
 * losing confidence in its own claims the moment someone commits to them —
 * a stable brand column beside a changing task column, which is also simpler
 * to keep correct in two languages.
 *
 * --- What this does not do ---
 *
 * It renders no credential fields itself — `children` is `LoginForm`,
 * `SignupForm` or `MfaEnrollment` (see `App.tsx`'s `Shell`), never built
 * in here.
 */
export function PreAuthScreen({
  screen,
  children,
}: {
  screen: PreAuthScreenKind;
  children: ReactNode;
}) {
  const { t } = useI18n();

  return (
    <main
      className={`pre-auth pre-auth--${screen}`}
      aria-label={t("auth.screen_label")}
      data-testid="pre-auth-screen"
      data-screen={screen}
    >
      <div className="pre-auth__panel">
        <header className="pre-auth__header">
          <Wordmark />
          <h1 className="pre-auth__heading" data-testid="pre-auth-heading">
            {t(HEADING_KEY[screen])}
          </h1>
        </header>

        <div className="pre-auth__body">{children}</div>

        {/*
          Deliberately after the form in the DOM, so a keyboard user reaches
          the thing they came for first — and still inside the screen, so it
          is a Tab away rather than somewhere else entirely.
        */}
        <footer className="pre-auth__language">
          {/*
            Appearance sits beside language for the same reason language is
            here at all (IAM-010g): both are "how this app is presented to
            me", both persist per device, and both have to be reachable by
            someone who has not signed in — a person who needs a dark screen
            needs it on the login page too, not from the moment they get an
            account.
          */}
          <div className="pre-auth__controls">
            <LanguageSwitcher />
            <ThemeToggle />
          </div>
          <p className="pre-auth__language-hint" data-testid="pre-auth-language-hint">
            {t("auth.language.hint")}
          </p>
        </footer>
      </div>

      {/*
        A real landmark, not decoration: this is the product's own pitch and
        belongs in the accessible tree. `forced-color-adjust: none` in
        app.css keeps the rail's fixed dark identity intact under Windows
        high-contrast, the same exception already carried for client markers
        (their colour is what they exist to show, not a themeable surface).
      */}
      <aside className="pre-auth__marketing" aria-label={t("auth.marketing.headline")}>
        <div className="pre-auth__marketing-brand">
          <Wordmark />
        </div>

        <h2 className="pre-auth__marketing-headline">{t("auth.marketing.headline")}</h2>
        <p className="pre-auth__marketing-subhead">{t("auth.marketing.subhead")}</p>

        <ul className="pre-auth__features">
          {FEATURES.map((feature) => (
            <li key={feature.titleKey} className="pre-auth__feature">
              {/*
                Padding/background/radius live on this wrapping span, not on
                the <svg> itself — app.css's global `box-sizing: border-box`
                reset applies to svg too, and padding directly on an svg with
                explicit width/height shrinks its CONTENT box by the padding
                amount rather than growing the element around it, which
                rendered every one of these as an near-invisible sliver
                inside an otherwise-empty tinted circle. Same wrapper shape
                `.home__item-icon` already uses (HomeScreen.tsx) for exactly
                this reason.
              */}
              <span className="pre-auth__feature-icon" aria-hidden="true">
                <svg
                  width="20"
                  height="20"
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  {feature.icon}
                </svg>
              </span>
              <div className="pre-auth__feature-text">
                <span className="pre-auth__feature-title">{t(feature.titleKey)}</span>
                <span className="pre-auth__feature-body">{t(feature.bodyKey)}</span>
              </div>
            </li>
          ))}
        </ul>
      </aside>
    </main>
  );
}
