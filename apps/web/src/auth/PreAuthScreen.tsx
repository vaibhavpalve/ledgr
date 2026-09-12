import type { ReactNode } from "react";
import { useI18n } from "@ledgr/i18n";

import { LanguageSwitcher } from "../LanguageSwitcher";
import { Wordmark } from "../Wordmark";

export type PreAuthScreenKind = "login" | "signup";

/**
 * The frame the login and signup screens render inside — IAM-010g,
 * FR-ONB-000.
 *
 *   IAM-010g   "Language is selectable BEFORE authentication, on the login and
 *              signup screens, defaulting to the browser or device locale and
 *              falling back to Dutch for `.nl` traffic. The choice persists on
 *              the device and is applied to the account after first login."
 *
 * --- Why the language control lives in the layout, not in each screen ---
 *
 * There are two pre-authentication screens and the requirement names both. A
 * control placed on each would be two controls to keep in step, and the one
 * that got forgotten would be on whichever screen shipped second. Putting it
 * in the frame both screens share makes "on the login and signup screens" a
 * consequence of the structure rather than a thing to remember.
 *
 * --- Why it is a footer and not a corner of the header ---
 *
 * This is the one screen in the product whose reader may not be able to read
 * it. The control is in the normal reading order with a visible label, rather
 * than an icon in a corner: somebody who opened a Dutch page expecting English
 * has to be able to FIND it, and a globe glyph is only discoverable to a
 * person already looking for one.
 *
 * The hint beside it says what will happen to the choice, because a person
 * choosing a language before they have an account has no way to know whether
 * it survives — and on a shared machine has a reason to care.
 *
 * --- What this does not do ---
 *
 * It renders no credential fields. The sign-in methods of IAM-010 (password,
 * Google, passkey) have no API endpoints yet, so the form is `children` and
 * the app passes a component that says so. Drawing a password field that
 * posts nowhere would be worse than an honest sentence.
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
      <header className="pre-auth__header">
        <Wordmark />
        <h1 className="pre-auth__heading" data-testid="pre-auth-heading">
          {t(screen === "login" ? "auth.sign_in.heading" : "auth.sign_up.heading")}
        </h1>
      </header>

      <div className="pre-auth__body">{children}</div>

      {/*
        Deliberately after the form in the DOM, so a keyboard user reaches the
        thing they came for first — and still inside the screen, so it is a
        Tab away rather than somewhere else entirely.
      */}
      <footer className="pre-auth__language">
        <LanguageSwitcher />
        <p className="pre-auth__language-hint" data-testid="pre-auth-language-hint">
          {t("auth.language.hint")}
        </p>
      </footer>
    </main>
  );
}
