import type { ReactNode } from "react";
import { Check, ReceiptText } from "lucide-react";
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
            <h1 className="pre-auth__heading bk-h" data-testid="pre-auth-heading">
              {t(HEADING_KEY[screen])}
              {screen === "login" ? ` ${t("auth.sign_in.title_mark")}` : null}
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

      <aside className="pre-auth__rail bk-on-forest" aria-label={t("auth.rail.headline_lead")}>
        <Logo size={22} onForest />

        <div className="pre-auth__rail-pitch">
          <h2 className="pre-auth__rail-headline bk-h">
            {t("auth.rail.headline_lead")}{" "}
            <span className="pre-auth__mark">{t("auth.rail.headline_mark")}</span>
          </h2>
          <p className="pre-auth__rail-sub">{t("auth.rail.subhead")}</p>
        </div>

        {/* A real product card: the receipt becomes this entry. Fixed light colours,
            since it is a picture of the product on the forest panel in either theme. */}
        <div className="pre-auth__demo" aria-hidden="true">
          <div className="bk-je pre-auth__journal">
            <div className="bk-je__head">
              <div className="pre-auth__receipt-id">
                <span className="pre-auth__receipt-icon">
                  <ReceiptText size={18} strokeWidth={1.75} />
                </span>
                <div>
                  <div className="pre-auth__receipt-vendor">{DEMO.vendor}</div>
                  <div className="pre-auth__receipt-meta">{t("auth.rail.demo_receipt_meta")}</div>
                </div>
              </div>
              <span className="bk-chip bk-chip--review">{t("auth.rail.demo_vat_detected")}</span>
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
                  <td className="r">52,80</td>
                  <td className="r">52,80</td>
                </tr>
              </tfoot>
            </table>
            <div className="bk-je__foot">
              <span className="bk-chip bk-chip--done">
                <Check strokeWidth={1.75} />
                {t("auth.rail.demo_balanced")}
              </span>
              <span className="pre-auth__auto">{t("auth.rail.demo_auto")}</span>
            </div>
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
