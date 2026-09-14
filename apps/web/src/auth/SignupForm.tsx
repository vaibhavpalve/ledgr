import { useState } from "react";
import { useI18n } from "@ledgr/i18n";

import type { AccountModel, AuthApi, AuthResult } from "./api";
import { errorMessage } from "./LoginForm";

/**
 * FR-MDL-001/FR-ONB-001a/b's signup form — one question (own business or
 * clients), branching into the two account models. `accountModel` gates
 * `kvkNumber` in the UI the same way the two account models gate it
 * server-side (`SignupBody.kvk_number` is optional and only meaningful for
 * `"firm"` — see `api.auth.signup.SignupService._create_organization`).
 *
 * No email verification and no Google-initiated signup here — ADR-054
 * names both as deliberate, bounded gaps, not omissions this form should
 * paper over. The Google button on `LoginForm` is honest about that: it
 * signs in an EXISTING Google-linked account only.
 */
export function SignupForm({
  api,
  onSwitchToLogin,
  onSignedUp,
}: {
  api: AuthApi;
  onSwitchToLogin: () => void;
  onSignedUp: (result: AuthResult) => void;
}) {
  const { t } = useI18n();
  const [accountModel, setAccountModel] = useState<AccountModel>("self_managed");
  const [organizationName, setOrganizationName] = useState("");
  const [kvkNumber, setKvkNumber] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  return (
    <form
      className="auth-form"
      data-testid="signup-form"
      aria-label={t("auth.sign_up.heading")}
      onSubmit={(event) => {
        event.preventDefault();
        setSubmitting(true);
        setProblem(null);
        void api
          .signup({
            accountModel,
            organizationName,
            kvkNumber: accountModel === "firm" ? kvkNumber : null,
            email,
            password,
          })
          .then(onSignedUp)
          .catch((error: unknown) => setProblem(errorMessage(error)))
          .finally(() => setSubmitting(false));
      }}
    >
      <fieldset>
        <legend>{t("auth.sign_up.account_model.label")}</legend>
        <label>
          <input
            type="radio"
            name="account-model"
            data-testid="signup-account-model-self-managed"
            checked={accountModel === "self_managed"}
            onChange={() => setAccountModel("self_managed")}
          />
          {t("auth.sign_up.account_model.self_managed")}
        </label>
        <label>
          <input
            type="radio"
            name="account-model"
            data-testid="signup-account-model-firm"
            checked={accountModel === "firm"}
            onChange={() => setAccountModel("firm")}
          />
          {t("auth.sign_up.account_model.firm")}
        </label>
      </fieldset>

      <label>
        {t("auth.sign_up.organization_name")}
        <input
          type="text"
          required
          data-testid="signup-organization-name"
          value={organizationName}
          onChange={(event) => setOrganizationName(event.target.value)}
        />
      </label>

      {accountModel === "firm" ? (
        <label>
          {t("auth.sign_up.kvk_number")}
          <input
            type="text"
            required
            data-testid="signup-kvk-number"
            value={kvkNumber}
            onChange={(event) => setKvkNumber(event.target.value)}
          />
        </label>
      ) : null}

      <label>
        {t("auth.sign_up.email")}
        <input
          type="email"
          autoComplete="username"
          required
          data-testid="signup-email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </label>

      <label>
        {t("auth.sign_up.password")}
        <input
          type="password"
          autoComplete="new-password"
          required
          data-testid="signup-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>

      {problem ? (
        <p role="alert" data-testid="signup-error">
          {problem}
        </p>
      ) : null}

      <button type="submit" data-testid="signup-submit" disabled={submitting}>
        {t("auth.sign_up.submit")}
      </button>

      {/* A different journey, not a second primary action — so it never wears
          a filled button beside the one the screen exists for. */}
      <button
        type="button"
        className="button--quiet"
        data-testid="switch-to-login"
        onClick={onSwitchToLogin}
      >
        {t("auth.sign_up.switch_to_login")}
      </button>
    </form>
  );
}
