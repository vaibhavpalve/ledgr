import type { ReactNode } from "react";
import { Check } from "lucide-react";
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
  address: "Herengracht 112",
  stamp: "18-09-2026 14:32",
  farewell: "PIN · BEDANKT",
  entryNumber: "0412",
  vat: "BTW 21%",
  total: "EUR 52,80",
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
      className={`pre-auth pre-auth--${screen} ui-root bk`}
      aria-label={t("auth.screen_label")}
      data-testid="pre-auth-screen"
      data-screen={screen}
    >
      <div className="pre-auth__panel">
        <div className="pre-auth__tools">
          <div className="pre-auth__controls">
            <LanguageSwitcher compact />
            <ThemeButton />
          </div>
        </div>

        <div className="pre-auth__column">
          <header className="pre-auth__header">
            {screen === "login" ? <p className="bk-over">{t("auth.sign_in.heading")}</p> : null}
            <h1 className="pre-auth__heading bk-serif" data-testid="pre-auth-heading">
              {t(HEADING_KEY[screen])}
              {screen === "login" ? (
                <>
                  {" "}
                  <em>{t("auth.sign_in.title_mark")}</em>
                </>
              ) : null}
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
          <p className="bk-over">
            {t("auth.footer.copyright", { year: new Date().getFullYear() })}
          </p>
        </footer>
      </div>

      <aside className="pre-auth__rail bk-on-pistachio" aria-label={t("auth.rail.headline_lead")}>
        <div className="pre-auth__rail-top">
          <Logo size={30} onPanel />
          <span className="bk-over pre-auth__rail-overline">{t("auth.rail.overline")}</span>
        </div>

        <div className="pre-auth__rail-pitch">
          <h2 className="pre-auth__rail-headline bk-serif">
            <span className="pre-auth__rail-lead">{t("auth.rail.headline_lead")}</span>
            <em className="pre-auth__mark bk-total">{t("auth.rail.headline_mark")}</em>
          </h2>
          <p className="pre-auth__rail-sub">{t("auth.rail.subhead")}</p>
        </div>

        <div className="pre-auth__demo" aria-hidden="true">
          <div className="pre-auth__receipt">
            <div className="bk-receipt">
              <div className="bk-receipt__sup">{DEMO.vendor}</div>
              <div>{DEMO.address}</div>
              <div>{DEMO.stamp}</div>
              <hr />
              <div className="bk-receipt__row">
                <span>{t("auth.rail.demo_item")}</span>
                <span>43,64</span>
              </div>
              <div className="bk-receipt__row">
                <span>{DEMO.vat}</span>
                <span>9,16</span>
              </div>
              <hr />
              <div className="bk-receipt__row pre-auth__receipt-total">
                <span>{t("auth.rail.demo_total")}</span>
                <span>{DEMO.total}</span>
              </div>
              <div className="pre-auth__receipt-farewell">{DEMO.farewell}</div>
            </div>
          </div>

          <svg
            className="pre-auth__arrow"
            viewBox="0 0 120 70"
            width="120"
            height="70"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M6 52 C 30 8, 78 4, 108 30" />
            <path d="M96 22 L 109 31 L 95 37" />
          </svg>

          <div className="bk-je bk-je--forest bk-je--tight pre-auth__journal">
            <div className="bk-je__head">
              <span className="bk-over">
                {t("auth.rail.demo_journal")} · {DEMO.entryNumber}
              </span>
              <span className="bk-chip pre-auth__booked">
                <Check size={12} strokeWidth={2.5} />
                {t("auth.rail.demo_booked")}
              </span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>{t("auth.rail.demo_account")}</th>
                  <th className="r">{t("auth.rail.demo_debit")}</th>
                  <th className="r">{t("auth.rail.demo_credit")}</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{DEMO.expense}</td>
                  <td className="r">43,64</td>
                  <td className="r" />
                </tr>
                <tr>
                  <td>{DEMO.vatAccount}</td>
                  <td className="r">9,16</td>
                  <td className="r" />
                </tr>
                <tr>
                  <td>{DEMO.bank}</td>
                  <td className="r" />
                  <td className="r">52,80</td>
                </tr>
              </tbody>
              <tfoot>
                <tr>
                  <td>{t("auth.rail.demo_total")}</td>
                  <td className="r">
                    <span className="bk-total bk-total--lemon">52,80</span>
                  </td>
                  <td className="r">
                    <span className="bk-total bk-total--lemon">52,80</span>
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>

          <svg className="bk-seal pre-auth__seal" viewBox="0 0 128 128" width="112" height="112">
            <defs>
              <path id="pre-auth-seal-path" d="M64 64m-47 0a47 47 0 1 1 94 0a47 47 0 1 1 -94 0" />
            </defs>
            <circle className="ring" cx="64" cy="64" r="64" />
            <text>
              <textPath href="#pre-auth-seal-path">{t("auth.rail.seal")}</textPath>
            </text>
            <circle className="core" cx="64" cy="64" r="27" />
            <path
              d="M52 64.5l8 8 16-17"
              fill="none"
              stroke="#14261b"
              strokeWidth="4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>

        <ul className="pre-auth__points">
          {POINTS.map((point, index) => (
            <li key={point} className="pre-auth__point">
              <div className="bk-over pre-auth__point-number">{`0${index + 1}`}</div>
              <div className="pre-auth__point-title">{t(`auth.rail.point_${point}_title`)}</div>
              <div className="pre-auth__point-body">{t(`auth.rail.point_${point}_body`)}</div>
            </li>
          ))}
        </ul>
      </aside>
    </main>
  );
}
