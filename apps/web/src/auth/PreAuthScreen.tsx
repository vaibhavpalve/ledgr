import type { ReactNode } from "react";
import { ArrowRight, Check } from "lucide-react";
import { useI18n } from "@ledgr/i18n";

import "./PreAuthScreen.css";
import { LanguageSwitcher } from "../LanguageSwitcher";
import { ThemeButton } from "../theme/ThemeButton";
import { Logo } from "../ui";

export type PreAuthScreenKind = "login" | "signup" | "mfa" | "recover" | "verify";

const HEADING_KEY: Record<PreAuthScreenKind, string> = {
  login: "auth.sign_in.title",
  signup: "auth.sign_up.heading",
  // ADR-054: shown to an already-authenticated caller who has not yet
  // cleared IAM-011's MFA gate - "mfa" is not one of IAM-010g's two named
  // screens, but reuses this same frame rather than inventing a second one,
  // since nothing about that layout is specific to signing in or signing up.
  mfa: "auth.mfa.heading",
  recover: "auth.recover.heading",
  // IAM-010b's link lands here. It used the login frame, so a person who had just
  // confirmed their address was greeted with "Welcome back - sign in".
  verify: "auth.verify_email.heading",
};

const POINTS = ["permanent", "exact", "passkey"] as const;

/**
 * The illustration's fixed, made-up sample: a receipt and the journal entry it
 * becomes. Account numbers and names are Dutch bookkeeping terms that stay
 * Dutch in both languages, so they are data here, not catalogue strings.
 */
const DEMO = {
  vendor: "Papierhuis Amsterdam",
  date: "18 Sep 2026",
  vat: "BTW 21%",
  expense: "4300 Kantoorkosten",
  vatAccount: "1520 BTW te vorderen",
  bank: "1100 Bank",
} as const;

/**
 * The frame the login, signup and MFA-enrolment screens render inside:
 * IAM-010g, FR-ONB-000, and the two-column layout of design/reference/Login.png
 * (brand panel left, task right).
 *
 *   IAM-010g   "Language is selectable BEFORE authentication, on the login and
 *              signup screens, defaulting to the browser or device locale and
 *              falling back to Dutch for `.nl` traffic. The choice persists on
 *              the device and is applied to the account after first login."
 *
 * --- DOM order is not the visual order ---
 *
 * `.pre-auth__panel` (the task) comes FIRST in the markup and the brand rail
 * second; `PreAuthScreen.css` places the rail on the left. A screen-reader or
 * keyboard user reaches the form before the pitch, and the rail stays a real
 * `aside` landmark rather than being hidden.
 *
 * --- The rail's copy is the same on every screen ---
 *
 * A pitch that changed mid-flow would read as the product losing confidence in
 * its own claims the moment someone commits to them. Its three points are each
 * a shipped property (FR-GL-003 permanent entries, NFR-031 decimal money,
 * IAM-012 passkey), not a claim made up for this screen. The receipt and the
 * journal entry are an illustration, not customer data.
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
      className={`pre-auth pre-auth--${screen} ui-root`}
      aria-label={t("auth.screen_label")}
      data-testid="pre-auth-screen"
      data-screen={screen}
    >
      <div className="pre-auth__panel">
        <div className="pre-auth__tools">
          <span className="pre-auth__tools-logo">
            <Logo />
          </span>
          <div className="pre-auth__controls">
            <LanguageSwitcher compact />
            <ThemeButton />
          </div>
        </div>

        <div className="pre-auth__column">
          <header className="pre-auth__header">
            <h1 className="pre-auth__heading" data-testid="pre-auth-heading">
              {t(HEADING_KEY[screen])}
            </h1>
            {screen === "login" || screen === "recover" ? (
              <p className="pre-auth__subtitle">
                {t(screen === "login" ? "auth.sign_in.subtitle" : "auth.recover.subtitle")}
              </p>
            ) : null}
          </header>

          <div className="pre-auth__body">{children}</div>
        </div>

        <footer className="pre-auth__footer">
          {/* What happens to the language choice, said once (IAM-010g). */}
          <p className="pre-auth__language-hint" data-testid="pre-auth-language-hint">
            {t("auth.language.hint")}
          </p>
          <p>{t("auth.footer.copyright", { year: new Date().getFullYear() })}</p>
        </footer>
      </div>

      <aside className="pre-auth__rail" aria-label={t("auth.rail.headline_lead")}>
        <svg className="pre-auth__rules" width="100%" height="900" fill="none" aria-hidden="true">
          <g stroke="var(--on-panel)" strokeOpacity="0.09" strokeWidth="1">
            {Array.from({ length: 12 }, (_, i) => (
              <line key={i} x1="0" x2="100%" y1={120 + i * 60} y2={120 + i * 60} />
            ))}
            <line x1="72" x2="72" y1="0" y2="900" />
          </g>
        </svg>
        <svg
          className="pre-auth__rings"
          width="410"
          height="410"
          viewBox="0 0 410 410"
          fill="none"
          aria-hidden="true"
        >
          <g stroke="var(--on-panel)" strokeOpacity="0.16" strokeWidth="1.5">
            <circle cx="410" cy="0" r="190" />
            <circle cx="410" cy="0" r="300" />
            <circle cx="410" cy="0" r="410" />
          </g>
          <circle cx="410" cy="0" r="80" fill="var(--accent)" />
        </svg>

        <div className="pre-auth__rail-brand">
          <Logo size={34} onPanel />
        </div>

        <div className="pre-auth__rail-pitch">
          <h2 className="pre-auth__rail-headline">
            {t("auth.rail.headline_lead")}{" "}
            <span className="pre-auth__mark">{t("auth.rail.headline_mark")}</span>
          </h2>
          <p className="pre-auth__rail-sub">{t("auth.rail.subhead")}</p>
        </div>

        <div className="pre-auth__demo" aria-hidden="true">
          <div className="pre-auth__receipt">
            <div className="pre-auth__receipt-vendor">{DEMO.vendor}</div>
            <div className="pre-auth__receipt-muted">{DEMO.date}</div>
            <div className="pre-auth__dash" />
            <div className="pre-auth__split">
              <span>{t("auth.rail.demo_item")}</span>
              <span>43,64</span>
            </div>
            <div className="pre-auth__split pre-auth__receipt-muted">
              <span>{DEMO.vat}</span>
              <span>9,16</span>
            </div>
            <div className="pre-auth__dash" />
            <div className="pre-auth__split pre-auth__receipt-total">
              <span>{t("auth.rail.demo_total")}</span>
              <span>€ 52,80</span>
            </div>
          </div>

          <div className="pre-auth__arrow">
            <ArrowRight size={22} strokeWidth={2.2} />
          </div>

          <div className="pre-auth__journal">
            <div className="pre-auth__journal-head">
              <span>{t("auth.rail.demo_journal")}</span>
              <span className="pre-auth__booked">
                <Check size={12} strokeWidth={3} />
                {t("auth.rail.demo_booked")}
              </span>
            </div>
            <div className="pre-auth__journal-grid">
              <span className="pre-auth__journal-muted">{t("auth.rail.demo_account")}</span>
              <span className="pre-auth__journal-muted pre-auth__figure">
                {t("auth.rail.demo_debit")}
              </span>
              <span className="pre-auth__journal-muted pre-auth__figure">
                {t("auth.rail.demo_credit")}
              </span>
              <span>{DEMO.expense}</span>
              <span className="pre-auth__figure pre-auth__mono">43,64</span>
              <span />
              <span>{DEMO.vatAccount}</span>
              <span className="pre-auth__figure pre-auth__mono">9,16</span>
              <span />
              <span>{DEMO.bank}</span>
              <span />
              <span className="pre-auth__figure pre-auth__mono">52,80</span>
            </div>
            <div className="pre-auth__journal-total">
              <span>{t("auth.rail.demo_total")}</span>
              <span className="pre-auth__figure pre-auth__mono">52,80</span>
              <span className="pre-auth__figure pre-auth__mono">52,80</span>
            </div>
          </div>

          <div className="pre-auth__chip pre-auth__chip--vat">{t("auth.rail.demo_chip_vat")}</div>
          <div className="pre-auth__chip pre-auth__chip--balanced">
            {t("auth.rail.demo_chip_balanced")}
          </div>
        </div>

        <ul className="pre-auth__points">
          {POINTS.map((point) => (
            <li key={point} className="pre-auth__point">
              <div className="pre-auth__point-title">{t(`auth.rail.point_${point}_title`)}</div>
              <div className="pre-auth__point-body">{t(`auth.rail.point_${point}_body`)}</div>
            </li>
          ))}
        </ul>
      </aside>
    </main>
  );
}
