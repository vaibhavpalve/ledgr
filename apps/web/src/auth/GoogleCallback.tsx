import { useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";

import type { AuthApi, AuthResult } from "./api";
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
  const [outcome, setOutcome] = useState<"pending" | "link_required" | "failed">("pending");
  const [linkMessage, setLinkMessage] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .loginGoogleCallback(code, state)
      .then((result) => {
        if (cancelled) return;
        if (result.kind === "signed_in") {
          onSignedIn(result.result);
        } else {
          setLinkMessage(result.message);
          setOutcome("link_required");
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setLinkMessage(errorMessage(error));
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

  return (
    <div data-testid="google-callback-problem">
      <p role="alert">
        {outcome === "link_required" ? linkMessage : t("auth.google.callback.failed")}
      </p>
      <button type="button" data-testid="google-callback-back" onClick={onBackToLogin}>
        {t("auth.google.callback.back_to_login")}
      </button>
    </div>
  );
}
