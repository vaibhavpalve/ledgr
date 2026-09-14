import { Outlet } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";

import { useAuth } from "../auth/AuthProvider";
import { PreAuthScreen } from "../auth/PreAuthScreen";
import { ServicesProvider, type Services } from "../session/ServicesProvider";
import { SessionProvider } from "../session/SessionProvider";
import { ErrorState, LoadingSkeleton } from "../shell/ScreenState";

/**
 * Everything behind sign-in hangs off this layout route: the API clients
 * (`ServicesProvider`), then the session bootstrap (`SessionProvider`),
 * then whatever route is nested. `RequireAuth` sits ABOVE it in the route
 * table, so by the time this renders there is a verified session to load.
 *
 * The two states `SessionProvider` cannot render children in — still
 * loading `/v1/me`, or unable to — are drawn here inside the pre-auth
 * frame, because there is no shell yet to draw them in: the shell needs
 * the session to know whose rail to show. The error state offers the two
 * honest ways out (D5): try again, or sign out and back in.
 */
export function AuthenticatedLayout({ services }: { services?: Partial<Services> }) {
  const { t } = useI18n();
  const { requestSignOut } = useAuth();

  return (
    <ServicesProvider overrides={services}>
      <SessionProvider
        renderLoading={() => (
          <div className="session-loading" data-testid="session-loading">
            <LoadingSkeleton rows={4} testId="session-loading-skeleton" />
          </div>
        )}
        renderError={(message, retry) => (
          <PreAuthScreen screen="login">
            <ErrorState message={message} onRetry={retry} testId="session-error">
              <button type="button" className="button--quiet" data-testid="sign-out" onClick={requestSignOut}>
                {t("auth.sign_out")}
              </button>
            </ErrorState>
            <p className="caption">{t("common.session.load_failed_hint")}</p>
          </PreAuthScreen>
        )}
      >
        <Outlet />
      </SessionProvider>
    </ServicesProvider>
  );
}
