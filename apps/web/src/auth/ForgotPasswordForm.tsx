import { useState } from "react";
import { CircleAlert } from "lucide-react";
import { useI18n } from "@ledgr/i18n";

import { Button, Field } from "../ui";
import type { AuthApi } from "./api";
import { errorMessage } from "./LoginForm";

/**
 * IAM-018's self-service recovery: a new password, on proof of the current
 * code from an authenticator the account already holds. An e-mail address alone
 * never recovers an account, which is why this asks for a code and does not send
 * a link. It signs no one in: every session is revoked server-side, so the
 * person is sent to the login screen to use the new password.
 *
 * An account with no authenticator cannot use this. The server answers that
 * exactly as it answers a wrong code, so the screen cannot be used to learn
 * which addresses have accounts.
 */
export function ForgotPasswordForm({
  api,
  onBackToLogin,
}: {
  api: AuthApi;
  onBackToLogin: () => void;
}) {
  const { t } = useI18n();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  if (done) {
    return (
      <div className="auth-form" data-testid="recover-done">
        <p role="status" className="login-switch">
          {t("auth.recover.done")}
        </p>
        <Button
          variant="primary"
          size="lg"
          block
          data-testid="recover-to-login"
          onClick={onBackToLogin}
        >
          {t("auth.recover.to_login")}
        </Button>
      </div>
    );
  }

  return (
    <div className="auth-form" data-testid="recover-form">
      <form
        className="login-actions"
        aria-label={t("auth.recover.heading")}
        onSubmit={(event) => {
          event.preventDefault();
          setSubmitting(true);
          setProblem(null);
          api
            .recover(email, code.replace(/\s+/g, ""), password)
            .then(() => setDone(true))
            .catch((error: unknown) => setProblem(errorMessage(error)))
            .finally(() => setSubmitting(false));
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
            data-testid="recover-email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <Field
            label={t("auth.recover.code")}
            helper={t("auth.recover.code_help")}
            type="text"
            inputMode="numeric"
            size="lg"
            autoComplete="one-time-code"
            required
            data-testid="recover-code"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
          <Field
            label={t("auth.recover.new_password")}
            type="password"
            size="lg"
            autoComplete="new-password"
            required
            data-testid="recover-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>

        {problem ? (
          <p role="alert" className="ui-error" data-testid="recover-error">
            <CircleAlert size={14} strokeWidth={1.7} aria-hidden="true" />
            <span>{problem}</span>
          </p>
        ) : null}

        <Button
          type="submit"
          variant="primary"
          size="lg"
          block
          data-testid="recover-submit"
          disabled={submitting}
        >
          {t("auth.recover.submit")}
        </Button>
      </form>

      <p className="login-switch">
        <button
          type="button"
          className="ui-textbutton"
          data-testid="recover-back"
          onClick={onBackToLogin}
        >
          {t("auth.recover.back")}
        </button>
      </p>
    </div>
  );
}
