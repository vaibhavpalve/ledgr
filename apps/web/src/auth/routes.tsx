import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";

import { describeError } from "../api/http";
import { AccountApi } from "../account/api";
import { intendedPath } from "../router/guards";
import { useAuth } from "./AuthProvider";
import { GoogleCallback } from "./GoogleCallback";
import { LoginForm } from "./LoginForm";
import { MfaEnrollment } from "./MfaEnrollment";
import { PreAuthScreen } from "./PreAuthScreen";
import { SignupForm } from "./SignupForm";

/**
 * The pre-authentication routes — one URL per screen, the forms themselves
 * unchanged from ADR-054. What used to be `Shell`'s "which screen" state is
 * now the URL, and "switch to signup" is a navigation rather than a state
 * flip; the form components still receive the same callbacks.
 *
 * Where a sign-in lands is decided ONCE, in `afterAuth`: the remembered URL
 * (`RequireAuth` stored it in `location.state.from`) or the dashboard when
 * MFA is satisfied, `/mfa` — carrying the same `from` — when it is not.
 */
function useAfterAuth() {
  const navigate = useNavigate();
  const location = useLocation();
  const { handleAuthResult } = useAuth();
  return (result: Parameters<typeof handleAuthResult>[0]) => {
    handleAuthResult(result);
    if (result.mfaVerified) {
      navigate(intendedPath(location.state), { replace: true });
    } else {
      navigate("/mfa", { replace: true, state: location.state });
    }
  };
}

export function LoginRoute() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const { authApi, notice, clearNotice } = useAuth();
  const afterAuth = useAfterAuth();

  return (
    <PreAuthScreen screen="login">
      {notice === "expired" ? (
        <p role="alert" className="alert alert--caution" data-testid="session-notice">
          {t("auth.session.expired")}
        </p>
      ) : null}
      <LoginForm
        api={authApi}
        onSwitchToSignup={() => {
          clearNotice();
          navigate("/signup", { state: location.state });
        }}
        onSignedIn={afterAuth}
        onGoogleStart={(authorizationUrl) => {
          // A full page navigation, not a fetch: this is Google's own
          // consent screen, outside this SPA entirely.
          window.location.href = authorizationUrl;
        }}
      />
    </PreAuthScreen>
  );
}

export function SignupRoute() {
  const navigate = useNavigate();
  const location = useLocation();
  const { authApi } = useAuth();
  const afterAuth = useAfterAuth();

  return (
    <PreAuthScreen screen="signup">
      <SignupForm
        api={authApi}
        onSwitchToLogin={() => navigate("/login", { state: location.state })}
        onSignedUp={afterAuth}
      />
    </PreAuthScreen>
  );
}

/**
 * ADR-054's gate. Reachable only while `status === "mfa_pending"`: a person
 * who reloads here starts over at `/login` (MFA verification does not
 * survive a reload — `auth/session.ts` says why), and one who is already
 * verified has no business here and is sent on.
 */
export function MfaRoute() {
  const navigate = useNavigate();
  const location = useLocation();
  const { authApi, status, pendingMfa } = useAuth();
  const afterAuth = useAfterAuth();

  useEffect(() => {
    if (status === "anonymous") navigate("/login", { replace: true, state: location.state });
    if (status === "authenticated") navigate(intendedPath(location.state), { replace: true });
  }, [status, navigate, location.state]);

  if (status !== "mfa_pending" || pendingMfa === null) return null;

  return (
    <PreAuthScreen screen="mfa">
      <MfaEnrollment api={authApi} enrollment={pendingMfa} onVerified={afterAuth} />
    </PreAuthScreen>
  );
}

/**
 * Google's redirect lands on `/` or `/login` with `?code&state` (the
 * registered redirect URI is the app's root — see the local dev stack
 * notes). `App` renders this INSTEAD of the route tree while those two
 * parameters are present, so the callback keeps working on either path
 * exactly as it did before there was a router.
 */
export function GoogleCallbackRoute({
  code,
  state,
  onDone,
}: {
  code: string;
  state: string;
  onDone: () => void;
}) {
  const navigate = useNavigate();
  const { authApi, handleAuthResult } = useAuth();

  return (
    <PreAuthScreen screen="login">
      <GoogleCallback
        api={authApi}
        code={code}
        state={state}
        onSignedIn={(result) => {
          handleAuthResult(result);
          onDone();
          navigate(result.mfaVerified ? "/" : "/mfa", { replace: true });
        }}
        onBackToLogin={() => {
          onDone();
          navigate("/login", { replace: true });
        }}
      />
    </PreAuthScreen>
  );
}

/**
 * IAM-010b: the link in the verification e-mail lands here with `?token=`.
 * Works signed in or out — the token identifies the address, not the
 * session — and says where to go next either way (D5).
 */
export function VerifyEmailRoute() {
  const { t, language } = useI18n();
  const [params] = useSearchParams();
  const { status, fetchImpl } = useAuth();
  const token = params.get("token");
  const [outcome, setOutcome] = useState<"pending" | "verified" | "failed" | "missing">(
    token === null ? "missing" : "pending",
  );
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (token === null) return;
    let cancelled = false;
    const api = new AccountApi({ language: () => language, fetchImpl });
    api
      .verifyEmail(token)
      .then(() => {
        if (!cancelled) setOutcome("verified");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setProblem(describeError(error));
        setOutcome("failed");
      });
    return () => {
      cancelled = true;
    };
    // The token is single-use: re-running on a language switch would burn it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  return (
    <PreAuthScreen screen="login">
      <div className="auth-form" data-testid="verify-email">
        <h2>{t("auth.verify_email.heading")}</h2>
        {outcome === "pending" ? (
          <p role="status" data-testid="verify-email-pending">
            {t("auth.verify_email.pending")}
          </p>
        ) : null}
        {outcome === "verified" ? (
          <p role="status" className="alert alert--positive" data-testid="verify-email-done">
            {t("auth.verify_email.done")}
          </p>
        ) : null}
        {outcome === "missing" ? (
          <p role="alert" className="alert alert--attention" data-testid="verify-email-missing">
            {t("auth.verify_email.missing_token")}
          </p>
        ) : null}
        {outcome === "failed" ? (
          <div role="alert" className="alert alert--attention" data-testid="verify-email-failed">
            <p>{problem}</p>
            <p>{t("auth.verify_email.failed_hint")}</p>
          </div>
        ) : null}
        <Link to={status === "authenticated" ? "/" : "/login"} className="auth-form__link">
          {status === "authenticated" ? t("auth.verify_email.continue") : t("auth.google.callback.back_to_login")}
        </Link>
      </div>
    </PreAuthScreen>
  );
}
