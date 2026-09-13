import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { I18nProvider, useI18n, type FormattingLocale, type Language } from "@ledgr/i18n";
import type { ClientBadge } from "@ledgr/shared-types";

import { LanguageSwitcher } from "./LanguageSwitcher";
import { MobileShell } from "./MobileShell";
import { SignOutConfirm } from "./SignOutConfirm";
import { Wordmark } from "./Wordmark";
import { AuthApi, type AuthResult, type MfaEnrollmentStatus } from "./auth/api";
import { GoogleCallback } from "./auth/GoogleCallback";
import { LoginForm } from "./auth/LoginForm";
import { MfaEnrollment } from "./auth/MfaEnrollment";
import { PreAuthScreen, type PreAuthScreenKind } from "./auth/PreAuthScreen";
import { clearSession, hasVerifiedStoredSession, storeSession } from "./auth/session";
import { SignupForm } from "./auth/SignupForm";
import { CaptureApi } from "./capture/api";
import { browserDecode } from "./capture/decode";
import { capturesAtRisk, captureQueue, purgeCaptureQueue } from "./capture/queue";
import { useSitting, type SittingContext } from "./capture/useSitting";
import { ClientApi } from "./client/api";
import { DashboardApi } from "./home/api";
import { applyAccountLanguage, initialLanguage, persistLanguage } from "./i18n";
import { SalesInvoiceApi } from "./invoicing/api";
import { useDocumentLanguage } from "./useDocumentLanguage";

/**
 * The shell every screen renders inside.
 *
 * --- Two settings, deliberately separate props (FR-LOC-002) ---
 *
 *   language           what the reader reads. Per user (FR-LOC-001b),
 *                      resolved from the device before the first paint
 *                      (IAM-010g) and replaced by the account's own setting
 *                      once there is one.
 *   formattingLocale   how figures are written. The OPEN ADMINISTRATION's,
 *                      so switching client changes how amounts read and
 *                      changes not one word of the interface.
 *
 * They are separate props rather than one because they change independently
 * and for different reasons: the first when a person clicks the language
 * control, the second when they switch client. Fusing them would make an
 * amount depend on who is reading it, which is precisely the failure
 * FR-LOC-002 is written to prevent.
 *
 * `formattingLocale` is a prop with a default rather than something this
 * component fetches: it comes from `GET /v1/switcher/active`'s administration
 * and there is no client for it yet. When one lands, the value is passed in
 * here and every figure in the tree follows it with no other change.
 *
 * --- `authenticated`/`screen` are controlled overrides, not the only source of truth ---
 *
 * ADR-054 wired IAM-010's endpoints and `Shell` now derives real state:
 * whether this browser holds a stored, MFA-verified session
 * (`auth/session.hasVerifiedStoredSession`), and which pre-auth screen is
 * showing (flipped by `LoginForm`/`SignupForm`'s own "switch" links). Both
 * props stay `undefined` by default so that happens — the same
 * controlled-with-uncontrolled-fallback shape a form input takes. Passing
 * either one explicitly (as the test suite does, to force a specific
 * screen with no token or fetch involved) overrides the derived value on
 * every render for as long as the prop keeps being passed; production
 * (`main.tsx`) never passes either, so the real session/screen state runs
 * the whole time.
 */
export function App({
  formattingLocale,
  language = initialLanguage(),
  authenticated,
  screen,
  mobileContext,
}: {
  formattingLocale?: FormattingLocale;
  language?: Language;
  authenticated?: boolean;
  screen?: PreAuthScreenKind;
  /**
   * The mobile shell's tenant context — the same seam `useSitting`'s own
   * `SittingContext` already is (see that module's docstring): supplied by
   * whatever knows the session, because there is no active-client endpoint
   * wired yet. Left `undefined`, the authenticated branch renders today's
   * bare header only — the pre-existing, already-documented gap, not a
   * regression introduced by the mobile shell.
   */
  mobileContext?: SittingContext;
} = {}) {
  // Persisting is a side effect of the switch, never a precondition for it.
  // FR-LOC-001a's "taking effect immediately without reload or
  // re-authentication" means the repaint does not wait for this and is not
  // undone if it fails — see persistLanguage.
  const onLanguageChange = useCallback((next: Language) => {
    void persistLanguage(next);
  }, []);

  return (
    <I18nProvider
      initialLanguage={language}
      formattingLocale={formattingLocale}
      onLanguageChange={onLanguageChange}
    >
      <Shell authenticated={authenticated} screen={screen} mobileContext={mobileContext} />
    </I18nProvider>
  );
}

/**
 * Inside the provider, because everything here needs the language that is
 * actually being rendered rather than the one the app started with.
 *
 * --- The four things this can be showing, in priority order ---
 *
 *   1. Google's OAuth redirect landing (`readGoogleCallbackParams`) — a
 *      one-time condition read from the URL at mount, never re-entered
 *      once handled.
 *   2. `MfaEnrollment` — a session exists (`pendingMfa !== null`) but has
 *      not cleared IAM-011's gate. ADR-054's whole point: this is not
 *      optional and there is no way to dismiss it, the same as
 *      `MfaEnforcementMiddleware` gives a route no opt-out.
 *   3. `LoginForm`/`SignupForm` — nobody is signed in.
 *   4. The authenticated shell.
 *
 * `pendingMfa` and `authenticated` are deliberately separate pieces of
 * state rather than one three-way enum: `authenticated` alone is what the
 * pre-existing test suite already controls via the prop (see `App`'s own
 * docstring), and folding the MFA gate into that same flag would mean a
 * test forcing `authenticated` would also have to know about a screen it
 * never asked for.
 */
function Shell({
  authenticated: authenticatedProp,
  screen: screenProp,
  mobileContext,
}: {
  authenticated?: boolean;
  screen?: PreAuthScreenKind;
  mobileContext?: SittingContext;
}) {
  const { language, adoptLanguage, t } = useI18n();

  // WCAG 2.2 SC 3.1.1 (FR-LOC-004). Part of "immediately": a switch that
  // repaints the words and leaves the page declaring itself English has
  // changed the product for sighted users only.
  useDocumentLanguage(language);

  const [authenticated, setAuthenticated] = useState(
    () => authenticatedProp ?? hasVerifiedStoredSession(),
  );
  useEffect(() => {
    if (authenticatedProp !== undefined) setAuthenticated(authenticatedProp);
  }, [authenticatedProp]);

  const [screen, setScreen] = useState<PreAuthScreenKind>(screenProp ?? "login");
  useEffect(() => {
    if (screenProp !== undefined) setScreen(screenProp);
  }, [screenProp]);

  const [pendingMfa, setPendingMfa] = useState<MfaEnrollmentStatus | null>(null);
  const [google, setGoogle] = useState(() => readGoogleCallbackParams());

  const authApi = useMemo(() => new AuthApi({ language: () => language }), [language]);

  useAccountLanguageOnFirstLogin(authenticated, language, adoptLanguage);

  // Every sign-in/sign-up/MFA path in ADR-054 answers the same shape
  // (AuthResult), so there is exactly one place that decides what a
  // caller sees next: straight into the app when MFA is already
  // satisfied (passkey sign-in, or a step-up verification), or the
  // enrolment gate otherwise (a fresh signup, or a password/Google
  // sign-in with no factor verified yet this session).
  const handleAuthResult = useCallback((result: AuthResult) => {
    storeSession({ accessToken: result.accessToken, mfaVerified: result.mfaVerified });
    // Clearing the Google-callback state here too (not just in
    // `onBackToLogin`) matters even for a sign-in that did NOT come from
    // Google: `google !== null` outranks every other branch below, so a
    // successful LoginForm/SignupForm/MfaEnrollment result would otherwise
    // leave `GoogleCallback` rendered forever underneath state that has
    // already moved on. A no-op when there was no callback to clear.
    setGoogle(null);
    clearGoogleCallbackParams();
    if (result.mfaVerified) {
      setPendingMfa(null);
      setAuthenticated(true);
    } else {
      setPendingMfa(result.enrollment ?? { hasPasskey: false, hasTotp: false });
    }
  }, []);

  // `null` = no prompt showing; a number = capturesAtRisk()'s answer, shown
  // in SignOutConfirm before the purge runs (MOB-009's warning).
  const [captureRisk, setCaptureRisk] = useState<number | null>(null);

  const performSignOut = useCallback(() => {
    // The client-side state is what actually controls what this browser
    // shows next; a failed logout call leaves nothing worse than a
    // session that later expires on its own (ADR-054's own named
    // limitation — see its Consequences on revocation not being
    // immediate), never a reason to leave the person stuck on a
    // "signing out…" screen.
    void authApi.logout().catch(() => {});
    clearSession();
    setPendingMfa(null);
    setAuthenticated(false);
    setCaptureRisk(null);
  }, [authApi]);

  // MOB-009's "logout" purge trigger. Checks capturesAtRisk() BEFORE
  // signing out rather than after, because the warning has to be seen
  // while there is still a choice to make - by the time performSignOut has
  // run there is no session left to warn from. Nothing at risk (the
  // ordinary case for a desktop-only session, or a mobile one with nothing
  // queued) skips SignOutConfirm entirely and signs out immediately, same
  // as before this existed.
  const handleSignOutClick = useCallback(() => {
    void capturesAtRisk().then((count) => {
      if (count > 0) {
        setCaptureRisk(count);
      } else {
        performSignOut();
      }
    });
  }, [performSignOut]);

  const handleConfirmSignOut = useCallback(() => {
    void purgeCaptureQueue("logout").then(performSignOut);
  }, [performSignOut]);

  if (google !== null) {
    return (
      <PreAuthScreen screen="login">
        <GoogleCallback
          api={authApi}
          code={google.code}
          state={google.state}
          onSignedIn={handleAuthResult}
          onBackToLogin={() => {
            clearGoogleCallbackParams();
            setGoogle(null);
            setScreen("login");
          }}
        />
      </PreAuthScreen>
    );
  }

  if (pendingMfa) {
    return (
      <PreAuthScreen screen="mfa">
        <MfaEnrollment api={authApi} enrollment={pendingMfa} onVerified={handleAuthResult} />
      </PreAuthScreen>
    );
  }

  if (!authenticated) {
    return (
      <PreAuthScreen screen={screen}>
        {screen === "login" ? (
          <LoginForm
            api={authApi}
            onSwitchToSignup={() => setScreen("signup")}
            onSignedIn={handleAuthResult}
            onGoogleStart={(authorizationUrl) => {
              // A full page navigation, not a fetch: this is Google's own
              // consent screen, outside this SPA entirely.
              window.location.href = authorizationUrl;
            }}
          />
        ) : (
          <SignupForm
            api={authApi}
            onSwitchToLogin={() => setScreen("login")}
            onSignedUp={handleAuthResult}
          />
        )}
      </PreAuthScreen>
    );
  }

  return (
    <div className="app">
      <header className="app__bar">
        <Wordmark />
        <LanguageSwitcher />
        <button type="button" data-testid="sign-out" onClick={handleSignOutClick}>
          {t("auth.sign_out")}
        </button>
      </header>
      {mobileContext !== undefined ? <AuthenticatedMobileShell context={mobileContext} /> : null}
      {captureRisk !== null ? (
        <SignOutConfirm
          count={captureRisk}
          onCancel={() => setCaptureRisk(null)}
          onConfirm={handleConfirmSignOut}
        />
      ) : null}
    </div>
  );
}

/** Google's redirect back to this app carries `?code=...&state=...` - read
 * once at mount (a `useState` initializer, not an effect) because the URL
 * this page loaded with does not change again while it stays mounted. */
function readGoogleCallbackParams(): { code: string; state: string } | null {
  if (typeof window === "undefined") return null;
  const search = new URLSearchParams(window.location.search);
  const code = search.get("code");
  const state = search.get("state");
  return code && state ? { code, state } : null;
}

function clearGoogleCallbackParams(): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.delete("code");
  url.searchParams.delete("state");
  window.history.replaceState(null, "", url.toString());
}

/**
 * Wires the mobile shell's dependencies together — the composition root for
 * the five tabs, mirroring `capture/queue.ts`'s own "one file that knows
 * which adapter fills which port" posture.
 *
 * `queue` and `decode` are the exact modules FR-EXP-001/MOB-002 already ship
 * (`captureQueue()`'s IndexedDB/WebCrypto composition, `browserDecode()`'s
 * canvas-based quality reading) — reused unchanged, not rebuilt for the
 * mobile shell.
 *
 * --- FR-FRM-000a: fetching the active-client badge, once, here ---
 *
 * This is the seam `MobileShell`'s own docstring names: the badge is fetched
 * exactly once, at this composition root, and handed down as a prop — never
 * re-fetched per tab and never fetched inside `MobileShell` itself.
 *
 * `badge` starts as `undefined` — NOT YET KNOWN — and only becomes `null`
 * once `GET /v1/switcher/active` actually answers with "no active client".
 * This distinction is the whole point: collapsing the pre-fetch state into
 * `null` would render `ClientHeader`'s "No client selected" for a moment on
 * every cold start, which is precisely the kind of momentary wrong signal
 * FR-FRM-000a exists to rule out. See `ClientHeader`'s own docstring and
 * ADR-049 for the full reasoning.
 *
 * A failed fetch (offline, a refusal) is caught and left as `undefined`
 * rather than advanced to `null` or a stale badge — an unanswered question is
 * not the same fact as "confirmed no client", and showing the neutral loading
 * state indefinitely is the safe failure mode for a header whose one job is
 * to never assert something false.
 */
function AuthenticatedMobileShell({ context }: { context: SittingContext }) {
  const { language } = useI18n();
  const queue = useMemo(() => captureQueue(), []);
  const decode = useMemo(() => browserDecode(), []);
  const captureApi = useMemo(() => new CaptureApi({ language: () => language }), [language]);
  const invoiceApi = useMemo(() => new SalesInvoiceApi({ language: () => language }), [language]);
  const dashboardApi = useMemo(() => new DashboardApi({ language: () => language }), [language]);
  const clientApi = useMemo(() => new ClientApi({ language: () => language }), [language]);
  const sitting = useSitting({ context, queue, api: captureApi });

  const [badge, setBadge] = useState<ClientBadge | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    setBadge(undefined);
    clientApi
      .fetchActiveBadge()
      .then((result) => {
        if (!cancelled) setBadge(result);
      })
      .catch(() => {
        // Left as `undefined` (the neutral loading state) — see this
        // function's own docstring for why that, not `null`, is the safe
        // failure mode here.
      });
    return () => {
      cancelled = true;
    };
  }, [clientApi]);

  return (
    <MobileShell
      administrationId={context.administrationId}
      fiscalYearId={context.fiscalYearId}
      badge={badge}
      sitting={sitting}
      queue={queue}
      decode={decode}
      captureApi={captureApi}
      invoiceApi={invoiceApi}
      dashboardApi={dashboardApi}
    />
  );
}

/**
 * IAM-010g's "applied to the account after first login."
 *
 * Runs on the TRANSITION into an authenticated session, not on every render
 * while signed in — otherwise a later in-app language change would be
 * overwritten by whatever the account said when the page loaded, and the
 * switcher would appear to snap back.
 *
 * That is the dependency array's job, and deliberately not a
 * "have we done this already" flag: a flag would also suppress the SECOND
 * sign-in in one page session, so somebody signing out and back in as a
 * colleague would keep the previous person's language. Depending on
 * `authenticated` alone gets both cases right — it does not re-run while the
 * session continues, and it does run again when a new one begins.
 *
 * `language` is read through a ref for the same reason it is absent from the
 * dependencies: reacting to it is exactly the loop described above.
 *
 * The result is applied through `adoptLanguage` rather than `setLanguage`:
 * the account already holds this value, and telling it again would be one
 * pointless round trip per sign-in on every machine whose remembered language
 * differs. `applyAccountLanguage` has already written whatever the DEVICE
 * needed. The repaint is identical either way.
 *
 * This matters when the account language differs from the device's — the
 * person signed in on a machine that remembered somebody else's choice, and
 * the account is authoritative (FR-LOC-001b).
 */
function useAccountLanguageOnFirstLogin(
  authenticated: boolean,
  language: Language,
  adoptLanguage: (next: Language) => void,
): void {
  const current = useRef(language);
  current.current = language;

  useEffect(() => {
    if (!authenticated) return;

    let cancelled = false;
    void applyAccountLanguage(current.current).then((resolved) => {
      // The session can end while this is in flight; applying a language to a
      // tree that has since signed out is harmless but pointless, and setting
      // state on an unmounted tree is not.
      if (!cancelled && resolved !== current.current) adoptLanguage(resolved);
    });

    return () => {
      cancelled = true;
    };
  }, [authenticated, adoptLanguage]);
}
