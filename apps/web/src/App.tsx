import { useCallback, useEffect, useRef, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { I18nProvider, useI18n, type FormattingLocale, type Language } from "@ledgr/i18n";

import { AuthProvider, useAuth } from "./auth/AuthProvider";
import {
  ForgotPasswordRoute,
  GoogleCallbackRoute,
  LoginRoute,
  MfaRoute,
  SignupRoute,
  VerifyEmailRoute,
} from "./auth/routes";
import { CaptureRoute } from "./capture/CaptureRoute";
import { ClientsScreen } from "./clients/ClientsScreen";
import { CustomerDetailScreen } from "./customers/CustomerDetailScreen";
import { CustomerFormScreen } from "./customers/CustomerFormScreen";
import { CustomerListScreen } from "./customers/CustomerListScreen";
import { DashboardRoute } from "./home/DashboardRoute";
import { applyAccountLanguage, initialLanguage, persistLanguage } from "./i18n";
import { InvoiceDetailScreen } from "./invoicing/InvoiceDetailScreen";
import { InvoiceListScreen } from "./invoicing/InvoiceListScreen";
import { NewInvoiceScreen } from "./invoicing/NewInvoiceScreen";
import { LedgerScreen } from "./ledger/LedgerScreen";
import { OnboardingRoute } from "./onboarding/OnboardingRoute";
import { OverviewRoute, ReviewRoute } from "./review/routes";
import { AuthenticatedLayout } from "./router/AuthenticatedLayout";
import { RedirectIfAuthenticated, RequireAdministration, RequireAuth } from "./router/guards";
import { NotFound } from "./router/NotFound";
import { Website } from "./site/Website";
import type { Services } from "./session/ServicesProvider";
import {
  AppearanceSettings,
  InvoiceDesignSettings,
  OrganizationSettings,
  ProfileSettings,
  SecuritySettings,
  SettingsLayout,
} from "./settings/SettingsScreens";
import { AppShell } from "./shell/AppShell";
import { ComingSoon } from "./shell/ComingSoon";
import { useDocumentLanguage } from "./useDocumentLanguage";

/**
 * The root of the web app.
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
 * --- What this is, since the router (ADR-058) ---
 *
 * One URL per screen. `Routes` below is the whole map of the product; the
 * three guards (`router/guards.tsx`) are layout routes, so a screen nested
 * under `RequireAdministration` cannot render without a tenant context by
 * construction rather than by remembering to check. `AuthProvider` holds
 * whether anyone is signed in; `AuthenticatedLayout` loads `GET /v1/me` and
 * gives every screen beneath it the session's administration and fiscal
 * year in memory (FR-WEB-006).
 *
 * `authenticated`, `services` and `baseFetch` are test seams, the same
 * controlled-with-uncontrolled-fallback shape the old `authenticated` prop
 * had: production (`main.tsx`) passes none of them.
 */
export function App({
  formattingLocale,
  language = initialLanguage(),
  authenticated,
  services,
  baseFetch,
}: {
  formattingLocale?: FormattingLocale;
  language?: Language;
  authenticated?: boolean;
  services?: Partial<Services>;
  baseFetch?: () => typeof fetch;
} = {}) {
  // Persisting is a side effect of the switch, never a precondition for it
  // (FR-LOC-001a) — see persistLanguage.
  const onLanguageChange = useCallback(
    (next: Language) => {
      void persistLanguage(next, baseFetch?.());
    },
    [baseFetch],
  );

  return (
    <I18nProvider
      initialLanguage={language}
      formattingLocale={formattingLocale}
      onLanguageChange={onLanguageChange}
    >
      <AuthProvider initialAuthenticated={authenticated} baseFetch={baseFetch}>
        <Routed services={services} />
      </AuthProvider>
    </I18nProvider>
  );
}

function Routed({ services }: { services?: Partial<Services> }) {
  const { language, adoptLanguage } = useI18n();
  const { status, fetchImpl } = useAuth();

  // WCAG 2.2 SC 3.1.1 (FR-LOC-004): the page declares the language it renders.
  useDocumentLanguage(language);
  useAccountLanguageOnFirstLogin(status === "authenticated", language, adoptLanguage, fetchImpl);

  // Google's redirect back to this app carries `?code=...&state=...` — read
  // once at mount (a `useState` initializer, not an effect) because the URL
  // this page loaded with does not change again while it stays mounted.
  const [google, setGoogle] = useState(() => readGoogleCallbackParams());
  if (google !== null && status !== "authenticated") {
    return (
      <GoogleCallbackRoute
        code={google.code}
        state={google.state}
        onDone={() => {
          clearGoogleCallbackParams();
          setGoogle(null);
        }}
      />
    );
  }

  return (
    <Routes>
      <Route element={<RedirectIfAuthenticated />}>
        <Route path="/login" element={<LoginRoute />} />
        <Route path="/signup" element={<SignupRoute />} />
        <Route path="/forgot-password" element={<ForgotPasswordRoute />} />
      </Route>
      {/* The public marketing page (ADR-080). "/" stays the signed-in home. */}
      <Route path="/welcome" element={<Website />} />
      <Route path="/mfa" element={<MfaRoute />} />
      <Route path="/verify-email" element={<VerifyEmailRoute />} />

      <Route element={<RequireAuth />}>
        <Route element={<AuthenticatedLayout services={services} />}>
          <Route path="/onboarding" element={<OnboardingRoute />} />

          <Route element={<AppShell />}>
            <Route path="/clients" element={<ClientsScreen />} />
            <Route path="/settings" element={<SettingsLayout />}>
              <Route index element={<ProfileSettings />} />
              <Route path="profile" element={<ProfileSettings />} />
              <Route path="appearance" element={<AppearanceSettings />} />
              <Route path="security" element={<SecuritySettings />} />
              <Route path="organization" element={<OrganizationSettings />} />
              <Route element={<RequireAdministration />}>
                <Route path="invoice-design" element={<InvoiceDesignSettings />} />
              </Route>
            </Route>

            <Route element={<RequireAdministration />}>
              <Route path="/" element={<DashboardRoute />} />
              <Route path="/capture" element={<CaptureRoute />} />
              <Route path="/review" element={<ReviewRoute />} />
              <Route path="/review/:expenseId" element={<ReviewRoute />} />
              <Route path="/overview" element={<OverviewRoute />} />
              <Route path="/invoices" element={<InvoiceListScreen />} />
              <Route path="/invoices/new" element={<NewInvoiceScreen />} />
              <Route path="/invoices/:invoiceId" element={<InvoiceDetailScreen />} />
              <Route path="/customers" element={<CustomerListScreen />} />
              <Route path="/customers/new" element={<CustomerFormScreen />} />
              <Route path="/customers/:customerId" element={<CustomerDetailScreen />} />
              <Route path="/customers/:customerId/edit" element={<CustomerFormScreen />} />
              <Route path="/ledger" element={<LedgerScreen />} />
              <Route path="/bank" element={<ComingSoon section="bank" />} />
              <Route path="/journal" element={<ComingSoon section="journal" />} />
              <Route path="/assets" element={<ComingSoon section="assets" />} />
              <Route path="/reports" element={<ComingSoon section="reports" />} />
            </Route>

            <Route path="*" element={<NotFound />} />
          </Route>
        </Route>
      </Route>
    </Routes>
  );
}

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
 * IAM-010g's "applied to the account after first login."
 *
 * Runs on the TRANSITION into an authenticated session, not on every render
 * while signed in — otherwise a later in-app language change would be
 * overwritten by whatever the account said when the page loaded. Depending
 * on `authenticated` alone gets both cases right: it does not re-run while
 * the session continues, and it does run again when a new one begins
 * (somebody signing out and back in as a colleague).
 *
 * `language` is read through a ref for the same reason it is absent from
 * the dependencies: reacting to it is exactly the loop described above.
 * The result is applied through `adoptLanguage` rather than `setLanguage`:
 * the account already holds this value, and telling it again would be one
 * pointless round trip per sign-in.
 */
function useAccountLanguageOnFirstLogin(
  authenticated: boolean,
  language: Language,
  adoptLanguage: (next: Language) => void,
  fetchImpl: typeof fetch,
): void {
  const current = useRef(language);
  current.current = language;

  useEffect(() => {
    if (!authenticated) return;

    let cancelled = false;
    void applyAccountLanguage(current.current, fetchImpl).then((resolved) => {
      if (!cancelled && resolved !== current.current) adoptLanguage(resolved);
    });

    return () => {
      cancelled = true;
    };
    // `fetchImpl` is stable for the life of an AuthProvider; listing it would
    // add nothing but is required for the lint to see the dependency honestly.
  }, [authenticated, adoptLanguage, fetchImpl]);
}
