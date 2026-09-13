import { useState } from "react";
import { useI18n } from "@ledgr/i18n";

import type { AuthApi, AuthResult } from "./api";
import { ApiError, OfflineError } from "./api";
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
  onSignedIn,
  onGoogleStart,
}: {
  api: AuthApi;
  onSwitchToSignup: () => void;
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
        aria-label={t("auth.sign_in.heading")}
        onSubmit={(event) => {
          event.preventDefault();
          void withProblem("password", async () => {
            onSignedIn(await api.login(email, password));
          });
        }}
      >
        <label>
          {t("auth.sign_in.email")}
          <input
            type="email"
            autoComplete="username"
            required
            data-testid="login-email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>

        <label>
          {t("auth.sign_in.password")}
          <input
            type="password"
            autoComplete="current-password"
            required
            data-testid="login-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>

        {problem ? (
          <p role="alert" data-testid="login-error">
            {problem}
          </p>
        ) : null}

        <button type="submit" data-testid="login-submit" disabled={submitting !== null}>
          {t("auth.sign_in.submit")}
        </button>
      </form>

      <p className="auth-form__divider">{t("auth.sign_in.or")}</p>

      <button
        type="button"
        data-testid="login-google"
        disabled={submitting !== null}
        onClick={() =>
          void withProblem("google", async () => {
            const { authorizationUrl } = await api.loginGoogleStart();
            onGoogleStart(authorizationUrl);
          })
        }
      >
        {t("auth.sign_in.google")}
      </button>

      <button
        type="button"
        data-testid="login-passkey"
        disabled={submitting !== null}
        onClick={() =>
          void withProblem("passkey", async () => {
            const { ceremonyId, optionsJson } = await api.loginPasskeyBegin();
            const credential = await getPasskey(optionsJson);
            onSignedIn(await api.loginPasskeyFinish(ceremonyId, credential));
          })
        }
      >
        {t("auth.sign_in.passkey")}
      </button>

      <button type="button" data-testid="switch-to-signup" onClick={onSwitchToSignup}>
        {t("auth.sign_in.switch_to_signup")}
      </button>
    </div>
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
