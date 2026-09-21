import { useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";

import type { AccountModel, AuthApi, AuthResult } from "./api";
import { errorMessage } from "./LoginForm";

/**
 * The landing leg of IAM-010's Google sign-in redirect (`login_google_start`
 * sends the browser to Google's own consent screen; Google sends it back to
 * wherever `google_redirect_uri` is configured to, with `?code=...&state=...`
 * in the query string).
 *
 * There is no router in this app (`App.tsx`'s `Shell` picks screens by
 * state, not URL) — `Shell` detects this leg the same way, by reading
 * `window.location.search` once at mount, and renders this component
 * instead of `LoginForm`/`SignupForm` when it finds both parameters. This
 * only works when `google_redirect_uri` points somewhere this SPA is
 * served from, which is the ordinary shape for a single-page app's OAuth
 * redirect URI and is a deployment/configuration fact, not something this
 * component can arrange.
 */
export function GoogleCallback({
  api,
  code,
  state,
  onSignedIn,
  onBackToLogin,
}: {
  api: AuthApi;
  code: string;
  state: string;
  onSignedIn: (result: AuthResult) => void;
  onBackToLogin: () => void;
}) {
  const { t } = useI18n();
  const [outcome, setOutcome] = useState<
    "pending" | "link_required" | "failed" | "signup_required"
  >("pending");
  const [message, setMessage] = useState("");
  const [signup, setSignup] = useState<{ ticket: string; email: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .loginGoogleCallback(code, state)
      .then((result) => {
        if (cancelled) return;
        if (result.kind === "signed_in") {
          onSignedIn(result.result);
        } else if (result.kind === "signup_required") {
          setSignup({ ticket: result.ticket, email: result.email });
          setOutcome("signup_required");
        } else {
          setMessage(result.message);
          setOutcome("link_required");
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setMessage(errorMessage(error));
        setOutcome("failed");
      });
    return () => {
      cancelled = true;
    };
    // code/state identify one, single-use ceremony - re-running this on
    // anything but a genuinely new callback would replay an already-consumed
    // authorization code against the server for nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (outcome === "pending") {
    return (
      <p role="status" data-testid="google-callback-pending">
        {t("auth.google.callback.verifying")}
      </p>
    );
  }

  if (outcome === "signup_required" && signup !== null) {
    return (
      <GoogleSignupForm
        api={api}
        ticket={signup.ticket}
        email={signup.email}
        onSignedUp={onSignedIn}
      />
    );
  }

  return (
    <div data-testid="google-callback-problem">
      <p role="alert">{outcome === "link_required" ? message : t("auth.google.callback.failed")}</p>
      <button type="button" data-testid="google-callback-back" onClick={onBackToLogin}>
        {t("auth.google.callback.back_to_login")}
      </button>
    </div>
  );
}

/**
 * FR-MDL-001's one question, for a Google identity
 * `GoogleSignInService.sign_in` already created a bare account for (see
 * `api.auth.routes`' module docstring) - this form never asks for email or
 * password, both already settled by the Google sign-in that got here.
 * `ticket` is single-use: a second submission (a retry after a network
 * blip, a double click) would fail with `errors.ceremony_not_found`, shown
 * like any other refusal rather than specially handled - there is nothing
 * this screen can offer beyond "try again from the sign-in screen" once
 * that ticket is gone.
 */
function GoogleSignupForm({
  api,
  ticket,
  email,
  onSignedUp,
}: {
  api: AuthApi;
  ticket: string;
  email: string;
  onSignedUp: (result: AuthResult) => void;
}) {
  const { t } = useI18n();
  const [accountModel, setAccountModel] = useState<AccountModel>("self_managed");
  const [organizationName, setOrganizationName] = useState("");
  const [kvkNumber, setKvkNumber] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  return (
    <form
      className="auth-form"
      data-testid="google-signup-form"
      aria-label={t("auth.google.signup.heading")}
      onSubmit={(event) => {
        event.preventDefault();
        setSubmitting(true);
        setProblem(null);
        void api
          .signupGoogle({
            ticket,
            accountModel,
            organizationName,
            kvkNumber: accountModel === "firm" ? kvkNumber : null,
          })
          .then(onSignedUp)
          .catch((error: unknown) => setProblem(errorMessage(error)))
          .finally(() => setSubmitting(false));
      }}
    >
      <h2>{t("auth.google.signup.heading")}</h2>
      <p data-testid="google-signup-email">{t("auth.google.signup.intro", { email })}</p>

      <fieldset className="account-model">
        <legend>{t("auth.sign_up.account_model.label")}</legend>
        <div className="account-model__options">
          <label className="account-model__option">
            <input
              type="radio"
              name="google-signup-account-model"
              data-testid="google-signup-account-model-self-managed"
              checked={accountModel === "self_managed"}
              onChange={() => setAccountModel("self_managed")}
            />
            {t("auth.sign_up.account_model.self_managed")}
          </label>
          <label className="account-model__option">
            <input
              type="radio"
              name="google-signup-account-model"
              data-testid="google-signup-account-model-firm"
              checked={accountModel === "firm"}
              onChange={() => setAccountModel("firm")}
            />
            {t("auth.sign_up.account_model.firm")}
          </label>
        </div>
        <p className="account-model__hint">
          {t(
            accountModel === "firm"
              ? "auth.sign_up.account_model.firm_hint"
              : "auth.sign_up.account_model.self_managed_hint",
          )}
        </p>
      </fieldset>

      <label>
        {t("auth.sign_up.organization_name")}
        <input
          type="text"
          required
          data-testid="google-signup-organization-name"
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
            data-testid="google-signup-kvk-number"
            value={kvkNumber}
            onChange={(event) => setKvkNumber(event.target.value)}
          />
        </label>
      ) : null}

      {problem ? (
        <p role="alert" data-testid="google-signup-error">
          {problem}
        </p>
      ) : null}

      <button type="submit" data-testid="google-signup-submit" disabled={submitting}>
        {t("auth.sign_up.submit")}
      </button>
    </form>
  );
}
