import { useState } from "react";
import { CircleAlert, KeyRound } from "lucide-react";
import { useI18n } from "@ledgr/i18n";

import type { AuthApi, AuthResult } from "./api";
import { ApiError, OfflineError } from "./api";
import { Button, Field } from "../ui";
import { getPasskey, PasskeyUnavailableError } from "./webauthn";

/**
 * IAM-010's sign-in screen: password, passkey and Google side by side.
 * Follows `capture/ExpenseForm.tsx`'s pattern (local `useState`, no form
 * library, the server's already-translated sentence shown as-is on
 * failure — FR-UX-007) rather than inventing a new one for this screen.
 *
 * Passkey sign-in is a single click with no fields of its own — IAM-012
 * already counts it as a full MFA factor, so a successful ceremony here
 * goes straight to `onSignedIn` with `mfaVerified: true` (see
 * `login_passkey_finish`'s own docstring in `api.auth.routes`), never
 * through `MfaEnrollment`.
 */
export function LoginForm({
  api,
  onSwitchToSignup,
  onForgotPassword,
  onSignedIn,
  onGoogleStart,
}: {
  api: AuthApi;
  onSwitchToSignup: () => void;
  /** IAM-018's recovery screen. Omitted, the link is not drawn. */
  onForgotPassword?: () => void;
  onSignedIn: (result: AuthResult) => void;
  /** Called with the URL to navigate to for Google sign-in — `Shell` owns
   * the actual `window.location` assignment, the same "the composition
   * root does the side effect, the form only asks for it" split as
   * `onSignedIn`. */
  onGoogleStart: (authorizationUrl: string) => void;
}) {
  const { t } = useI18n();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState<"password" | "google" | "passkey" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [showPassword, setShowPassword] = useState(false);

  // `action` is typed `() => unknown` rather than `() => Promise<void>`
  // deliberately: `await` accepts any return type, and spelling out
  // `Promise<...>` in a .tsx file trips scripts/check_translations.py's
  // JSX-text heuristic (a bare `>` from `=>` immediately followed by a
  // capitalised word and a `<` reads exactly like untranslated JSX text).
  const withProblem = async (kind: "password" | "google" | "passkey", action: () => unknown) => {
    setSubmitting(kind);
    setProblem(null);
    try {
      await action();
    } catch (error) {
      setProblem(errorMessage(error));
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <div className="auth-form" data-testid="login-form">
      <form
        className="login-actions"
        aria-label={t("auth.sign_in.heading")}
        onSubmit={(event) => {
          event.preventDefault();
          void withProblem("password", async () => {
            onSignedIn(await api.login(email, password));
          });
        }}
      >
        <div className="login-fields">
          <Field
            label={t("auth.sign_in.email")}
            type="email"
            size="lg"
            autoComplete="username"
            placeholder={t("auth.sign_in.email_placeholder")}
            required
            data-testid="login-email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />

          <Field
            id="login-password"
            label={t("auth.sign_in.password")}
            type={showPassword ? "text" : "password"}
            size="lg"
            autoComplete="current-password"
            placeholder={t("auth.sign_in.password_placeholder")}
            required
            data-testid="login-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            labelAction={
              <button
                type="button"
                className="ui-textbutton"
                aria-controls="login-password"
                data-testid="login-show-password"
                onClick={() => setShowPassword((shown) => !shown)}
              >
                {t(showPassword ? "auth.sign_in.hide_password" : "auth.sign_in.show_password")}
              </button>
            }
          />
        </div>
        {onForgotPassword ? (
          <div className="login-forgot">
            <button
              type="button"
              className="ui-textbutton"
              data-testid="login-forgot"
              onClick={onForgotPassword}
            >
              {t("auth.sign_in.forgot")}
            </button>
          </div>
        ) : null}

        {problem ? (
          <p role="alert" className="ui-error" data-testid="login-error">
            <CircleAlert size={14} strokeWidth={1.7} aria-hidden="true" />
            <span>{problem}</span>
          </p>
        ) : null}

        <Button
          type="submit"
          variant="primary"
          size="lg"
          block
          data-testid="login-submit"
          disabled={submitting !== null}
        >
          {t("auth.sign_in.submit")}
        </Button>
      </form>

      <div className="login-actions">
        <p className="login-divider">{t("auth.sign_in.or_continue")}</p>

        <div className="login-alt">
          <Button
            size="lg"
            data-testid="login-google"
            aria-label={t("auth.sign_in.google")}
            disabled={submitting !== null}
            onClick={() =>
              void withProblem("google", async () => {
                const { authorizationUrl } = await api.loginGoogleStart();
                onGoogleStart(authorizationUrl);
              })
            }
          >
            <GoogleIcon />
            {t("auth.sign_in.google_short")}
          </Button>

          <Button
            size="lg"
            data-testid="login-passkey"
            aria-label={t("auth.sign_in.passkey")}
            disabled={submitting !== null}
            onClick={() =>
              void withProblem("passkey", async () => {
                const { ceremonyId, optionsJson } = await api.loginPasskeyBegin();
                const credential = await getPasskey(optionsJson);
                onSignedIn(await api.loginPasskeyFinish(ceremonyId, credential));
              })
            }
          >
            <KeyRound size={18} strokeWidth={1.8} aria-hidden="true" />
            {t("auth.sign_in.passkey_short")}
          </Button>
        </div>
      </div>

      {/* A different journey, not a second primary action, so it is a text
          link and never a filled button beside the one the screen exists for. */}
      <p className="login-switch">
        {t("auth.sign_in.no_account")}{" "}
        <button
          type="button"
          className="ui-textbutton"
          data-testid="switch-to-signup"
          onClick={onSwitchToSignup}
        >
          {t("auth.sign_in.create_one")}
        </button>
      </p>
    </div>
  );
}
/**
 * Google's own four-colour "G" mark, at the fixed proportions and colours
 * Google's brand guidelines require for a "Sign in with Google" button —
 * literal hexes, not `var(--ledgr-*)` tokens, for the same reason the client
 * marker colours and the pre-auth marketing rail are literals rather than
 * theme-following ones (see tokens.css and ADR-057): this mark's colour IS
 * what identifies it as Google's, and it has to render identically
 * regardless of LEDGR's own light/dark setting.
 *
 * `aria-hidden` — the button's own visible text already names the action
 * fully ("Sign in with Google"), so this is decoration, not a second
 * announcement.
 */
function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z"
      />
      <path
        fill="#FBBC05"
        d="M3.964 10.71c-.18-.54-.282-1.117-.282-1.71s.102-1.17.282-1.71V4.958H.957C.347 6.173 0 7.548 0 9s.348 2.827.957 4.042l3.007-2.332z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"
      />
    </svg>
  );
}

/** Shared with `SignupForm`/`MfaEnrollment`: the server's own sentence for
 * an `ApiError` (FR-UX-007 already translated it), a catalogue-free network
 * message for `OfflineError` (there is no server response to translate),
 * and the same for a WebAuthn ceremony the browser itself refused. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof OfflineError) return error.message;
  if (error instanceof PasskeyUnavailableError) return error.message;
  return error instanceof Error ? error.message : String(error);
}
