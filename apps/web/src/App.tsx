import { Suspense, lazy, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Navigate, Route, Routes, useParams } from "react-router-dom";
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
import { AssetsScreen } from "./assets/AssetsScreen";
import { BankFeedReturn } from "./bank/BankFeed";
import { BankScreen } from "./bank/BankScreen";
import { ClientsScreen } from "./clients/ClientsScreen";
import { CustomerDetailScreen } from "./customers/CustomerDetailScreen";
import { CustomerFormScreen } from "./customers/CustomerFormScreen";
import { CustomerListScreen } from "./customers/CustomerListScreen";
import { ClientRulesRoute } from "./firm/ClientRulesScreen";
import { FirmHomeRoute } from "./firm/FirmHomeScreen";
import { FirmInboxRoute } from "./firm/FirmInboxScreen";
import { DashboardRoute } from "./home/DashboardRoute";
import { applyAccountLanguage, initialLanguage, persistLanguage } from "./i18n";
import { InvoiceDetailScreen } from "./invoicing/InvoiceDetailScreen";
import { InvoiceListScreen } from "./invoicing/InvoiceListScreen";
import { NewInvoiceScreen } from "./invoicing/NewInvoiceScreen";
import { JournalScreen } from "./journal/JournalScreen";
import { LedgerScreen } from "./ledger/LedgerScreen";
import { OnboardingRoute } from "./onboarding/OnboardingRoute";
import { PurchasesRoute } from "./purchases/PurchasesRoute";
import { QuestionsRoute } from "./questions/QuestionsScreen";
import { ReceiptsNeededScreen } from "./receipts/ReceiptsNeededScreen";
import { ReportsScreen } from "./reports/ReportsScreen";
import { VatRoute } from "./vat/VatScreen";
import { OpeningBalanceScreen } from "./opening/OpeningBalanceScreen";
import { AuthenticatedLayout } from "./router/AuthenticatedLayout";
import { RedirectIfAuthenticated, RequireAdministration, RequireAuth } from "./router/guards";
import { NotFound } from "./router/NotFound";
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
      {/* The public site (design/site, ADR-107). "/" is its home for a visitor who has never
          signed in in this tab, and the dashboard for everyone else (RequireAuth below). */}
      <Route element={<Site />}>
        <Route path="/welcome" element={<Navigate to="/" replace />} />
        <Route path="/product" element={<SitePage name="ProductPage" />} />
        <Route path="/accountants" element={<SitePage name="AccountantsPage" />} />
        <Route path="/pricing" element={<SitePage name="PricingPage" />} />
        <Route path="/security" element={<SitePage name="SecurityPage" />} />
        <Route path="/demo" element={<SitePage name="DemoPage" />} />
        <Route path="/contact" element={<SitePage name="DemoPage" />} />
        <Route path="/articles" element={<SitePage name="ArticlesPage" />} />
        <Route path="/articles/:slug" element={<SitePage name="ArticlePage" />} />
        <Route path="/privacy" element={<SitePage name="PrivacyPage" />} />
        <Route path="/cookies" element={<SitePage name="CookiesPage" />} />
        <Route path="/responsible-disclosure" element={<SitePage name="DisclosurePage" />} />
      </Route>
      <Route path="/mfa" element={<MfaRoute />} />
      <Route path="/verify-email" element={<VerifyEmailRoute />} />

      <Route element={<RequireAuth publicHome={<SiteHome />} />}>
        <Route element={<AuthenticatedLayout services={services} />}>
          <Route path="/onboarding" element={<OnboardingRoute />} />

          <Route element={<AppShell />}>
            <Route path="/clients" element={<ClientsScreen />} />
            {/* The firm home: where a firm user lands with no client open (docs/firm-home). */}
            <Route path="/todo" element={<FirmHomeRoute />} />
            <Route path="/inbox" element={<FirmInboxRoute />} />
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
              <Route path="/purchases" element={<PurchasesRoute />} />
              <Route path="/purchases/:expenseId" element={<PurchasesRoute />} />
              {/* Capture, Review and Overview were three screens for what is now
                  one. The old addresses still work - bookmarks, the dashboard's
                  links, a support link somebody sent - and land on it. */}
              <Route path="/capture" element={<Navigate to="/purchases" replace />} />
              <Route path="/review" element={<Navigate to="/purchases" replace />} />
              <Route path="/review/:expenseId" element={<RedirectToPurchase />} />
              <Route path="/overview" element={<Navigate to="/purchases" replace />} />
              <Route path="/invoices" element={<InvoiceListScreen />} />
              <Route path="/invoices/new" element={<NewInvoiceScreen />} />
              <Route path="/invoices/:invoiceId" element={<InvoiceDetailScreen />} />
              <Route path="/customers" element={<CustomerListScreen />} />
              <Route path="/customers/new" element={<CustomerFormScreen />} />
              <Route path="/customers/:customerId" element={<CustomerDetailScreen />} />
              <Route path="/customers/:customerId/edit" element={<CustomerFormScreen />} />
              <Route path="/ledger" element={<LedgerScreen />} />
              <Route path="/ledger/opening-balance" element={<OpeningBalanceScreen />} />
              <Route path="/bank" element={<BankScreen />} />
              {/* Where the bank sends the person back after giving consent (ADR-108). */}
              <Route path="/bank/feed-return" element={<BankFeedReturn />} />
              <Route path="/journal" element={<JournalScreen />} />
              <Route path="/assets" element={<AssetsScreen />} />
              <Route path="/reports" element={<ReportsScreen />} />
              <Route path="/vat" element={<VatRoute />} />
              <Route path="/vat/:periodId" element={<VatRoute />} />
              {/* FR-FRM-005 question threads and the missing-receipts list (contract-wave2). */}
              <Route path="/questions" element={<QuestionsRoute />} />
              <Route path="/questions/:threadId" element={<QuestionsRoute />} />
              <Route path="/receipts-needed" element={<ReceiptsNeededScreen />} />
              {/* One client's approval rules and receipt reminders (contract-wave2, ADR-113/114). */}
              <Route path="/rules" element={<ClientRulesRoute />} />
            </Route>

            <Route path="*" element={<NotFound />} />
          </Route>
        </Route>
      </Route>
    </Routes>
  );
}

/**
 * The public site, loaded only when someone opens it: a signed-in bookkeeper never downloads the
 * marketing pages, and a visitor reading an article never downloads the ledger (ADR-107).
 */
const siteModule = () => import("./marketing/routes");
const LazySiteLayout = lazy(() => siteModule().then((m) => ({ default: m.SiteLayout })));
const LazySitePages = lazy(() => siteModule().then((m) => ({ default: m.SitePageByName })));
const LazySiteHome = lazy(() => siteModule().then((m) => ({ default: m.SiteHome })));

function SiteSuspense({ children }: { children: ReactNode }) {
  return <Suspense fallback={<div className="site-loading" />}>{children}</Suspense>;
}

function Site() {
  return (
    <SiteSuspense>
      <LazySiteLayout />
    </SiteSuspense>
  );
}

function SitePage({ name }: { name: string }) {
  return (
    <SiteSuspense>
      <LazySitePages name={name} />
    </SiteSuspense>
  );
}

function SiteHome() {
  return (
    <SiteSuspense>
      <LazySiteHome />
    </SiteSuspense>
  );
}

/** `/review/:id` was one draft opened for review; that is `/purchases/:id` now. */
function RedirectToPurchase() {
  const { expenseId } = useParams();
  return <Navigate to={`/purchases/${encodeURIComponent(expenseId ?? "")}`} replace />;
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
