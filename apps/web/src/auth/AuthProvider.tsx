import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useI18n } from "@ledgr/i18n";

import { AuthApi, type AuthResult, type MfaEnrollmentStatus } from "./api";
import { clearSession, hasVerifiedStoredSession, storeSession, storeTrustedDeviceToken } from "./session";
import { capturesAtRisk, purgeCaptureQueue } from "../capture/queue";
import { createAuthenticatedFetch } from "../session/authenticatedFetch";
import { SignOutConfirm } from "../SignOutConfirm";

/**
 * Whether anyone is signed in — the state `App.tsx`'s `Shell` used to hold
 * in two booleans, lifted into a provider so that a route guard, the
 * sign-out control in the user menu and the 401 handling in every API call
 * read ONE fact rather than three copies of it.
 *
 * --- Three states, and the reason they are one enum now ---
 *
 *   anonymous       nobody is signed in. Every route behind `RequireAuth`
 *                   redirects to `/login`, remembering where it was going.
 *   mfa_pending     a real, tenant-scoped session exists but has not cleared
 *                   IAM-011's gate (ADR-054). There is no way past `/mfa`
 *                   except through it — the same "no opt-out" the middleware
 *                   gives a route.
 *   authenticated   the session is verified. `SessionProvider` takes over
 *                   from here and loads `GET /v1/me`.
 *
 * `Shell` kept `authenticated` and `pendingMfa` as separate pieces of state so
 * that its test suite could force one without knowing about the other; with
 * a router, the URL is what a test forces, and one enum is what stops the
 * two from ever disagreeing (a session both verified and pending).
 *
 * --- What ends a session, and what it looks like ---
 *
 * Signing out (`requestSignOut`), or any API call answering 401
 * (`sessionEnded`). Both clear the stored token and drop to `anonymous`;
 * the second also sets `notice` so `/login` can say WHY the person is
 * looking at it again ("your session expired") rather than presenting a
 * bare form to someone who was mid-task a second ago (D5). MOB-009's
 * purge-on-logout warning stays exactly where ADR-054 put it: checked
 * BEFORE the sign-out, while there is still a choice to make.
 *
 * In-memory session state (`SessionProvider`, the screens' own data) is
 * cleared by unmounting: everything authenticated renders under
 * `RequireAuth`, which stops rendering it the moment `status` changes.
 * Nothing financial is in browser storage to begin with (FR-WEB-006).
 */
export type AuthStatus = "anonymous" | "mfa_pending" | "authenticated";

/** Why `/login` is being shown to someone who did not choose to sign out. */
export type SessionNotice = "expired";

export interface AuthContextValue {
  readonly status: AuthStatus;
  readonly pendingMfa: MfaEnrollmentStatus | null;
  readonly notice: SessionNotice | null;
  readonly authApi: AuthApi;
  /**
   * The `fetch` every authenticated client is built on: carries the bearer
   * token, and ends the session on a 401. See `session/authenticatedFetch`.
   */
  readonly fetchImpl: typeof fetch;
  clearNotice(): void;
  handleAuthResult(result: AuthResult): void;
  requestSignOut(): void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({
  initialAuthenticated,
  baseFetch,
  children,
}: {
  /**
   * Test override, the same controlled-with-uncontrolled-fallback shape
   * `App`'s old `authenticated` prop had. Production never passes it, so the
   * stored session decides.
   */
  initialAuthenticated?: boolean;
  /** Test seam; production resolves `globalThis.fetch` at call time. */
  baseFetch?: () => typeof fetch;
  children: ReactNode;
}) {
  const { language } = useI18n();
  const [status, setStatus] = useState<AuthStatus>(() =>
    (initialAuthenticated ?? hasVerifiedStoredSession()) ? "authenticated" : "anonymous",
  );
  const [pendingMfa, setPendingMfa] = useState<MfaEnrollmentStatus | null>(null);
  const [notice, setNotice] = useState<SessionNotice | null>(null);
  // `null` = no prompt showing; a number = capturesAtRisk()'s answer, shown
  // in SignOutConfirm before the purge runs (MOB-009's warning).
  const [captureRisk, setCaptureRisk] = useState<number | null>(null);

  const authApi = useMemo(
    () => new AuthApi({ language: () => language, fetchImpl: baseFetch?.() }),
    [language, baseFetch],
  );

  // Several calls can 401 in the same tick (a dashboard and a header both
  // loading). The first one ends the session; the rest are no-ops, which a
  // ref makes true synchronously rather than a render later.
  const ending = useRef(false);
  const sessionEnded = useCallback(() => {
    if (ending.current) return;
    ending.current = true;
    clearSession();
    setPendingMfa(null);
    setNotice("expired");
    setStatus("anonymous");
  }, []);

  const fetchImpl = useMemo(
    () => createAuthenticatedFetch({ base: baseFetch, onUnauthorized: sessionEnded }),
    [baseFetch, sessionEnded],
  );

  // Every sign-in/sign-up/MFA path in ADR-054 answers the same shape
  // (AuthResult), so there is exactly one place that decides what a caller
  // sees next: straight into the app when MFA is already satisfied (passkey
  // sign-in, or a step-up verification), or the enrolment gate otherwise.
  const handleAuthResult = useCallback((result: AuthResult) => {
    storeSession({ accessToken: result.accessToken, mfaVerified: result.mfaVerified });
    // ADR-061: present only when this MFA verification was asked to
    // remember the device - see AuthResult.trustedDeviceToken's own
    // docstring for why login()'s own responses never carry one.
    if (result.trustedDeviceToken !== null) {
      storeTrustedDeviceToken(result.trustedDeviceToken);
    }
    ending.current = false;
    setNotice(null);
    if (result.mfaVerified) {
      setPendingMfa(null);
      setStatus("authenticated");
    } else {
      setPendingMfa(result.enrollment ?? { hasPasskey: false, hasTotp: false });
      setStatus("mfa_pending");
    }
  }, []);

  const performSignOut = useCallback(() => {
    // The client-side state is what actually controls what this browser
    // shows next; a failed logout call leaves nothing worse than a session
    // that later expires on its own — never a reason to leave the person
    // stuck on a "signing out…" screen.
    void authApi.logout().catch(() => {});
    clearSession();
    ending.current = false;
    setPendingMfa(null);
    setNotice(null);
    setCaptureRisk(null);
    setStatus("anonymous");
  }, [authApi]);

  // MOB-009's "logout" purge trigger — the warning has to be seen while
  // there is still a choice to make, so the count is read before anything
  // is signed out. Nothing at risk skips the dialog entirely.
  const requestSignOut = useCallback(() => {
    void capturesAtRisk()
      .then((count) => {
        if (count > 0) setCaptureRisk(count);
        else performSignOut();
      })
      .catch(performSignOut);
  }, [performSignOut]);

  const clearNotice = useCallback(() => setNotice(null), []);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      pendingMfa,
      notice,
      authApi,
      fetchImpl,
      clearNotice,
      handleAuthResult,
      requestSignOut,
    }),
    [status, pendingMfa, notice, authApi, fetchImpl, clearNotice, handleAuthResult, requestSignOut],
  );

  return (
    <AuthContext.Provider value={value}>
      {children}
      {captureRisk !== null ? (
        <SignOutConfirm
          count={captureRisk}
          onCancel={() => setCaptureRisk(null)}
          onConfirm={() => void purgeCaptureQueue("logout").then(performSignOut, performSignOut)}
        />
      ) : null}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth() outside an <AuthProvider>.");
  }
  return value;
}
