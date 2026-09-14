import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useI18n } from "@ledgr/i18n";
import type { CaptureQueue } from "@ledgr/offline-queue";

import { AccountApi } from "../account/api";
import { useAuth } from "../auth/AuthProvider";
import { CaptureApi } from "../capture/api";
import { browserDecode, type DecodeFile } from "../capture/decode";
import { captureQueue } from "../capture/queue";
import { ClientApi } from "../client/api";
import { CustomerApi } from "../customers/api";
import { DashboardApi } from "../home/api";
import { SalesInvoiceApi } from "../invoicing/api";
import { LedgerApi } from "../ledger/api";
import { OnboardingApi } from "../onboarding/api";
import { TemplateApi } from "../templates/api";

/**
 * The composition root for every API client the authenticated app uses —
 * what `AuthenticatedMobileShell` in the old `App.tsx` was for five tabs,
 * for the whole product.
 *
 * One instance of each per LANGUAGE, because every client reads
 * `Accept-Language` through `language()` at call time and FR-UX-007 wants a
 * refusal in the language on screen; and all of them on the SAME
 * authenticated `fetch` (`AuthProvider.fetchImpl`), which is the one place
 * the bearer token and the 401 handling live.
 *
 * `queue` and `decode` are the exact modules FR-EXP-001/MOB-002 already ship
 * (`captureQueue()`'s IndexedDB/WebCrypto composition, `browserDecode()`'s
 * canvas-based quality reading) — reused unchanged, not rebuilt for the
 * routed app. `overrides` is the test seam: a screen test hands in a fake
 * `CustomerApi` and never opens IndexedDB.
 */
export interface Services {
  readonly account: AccountApi;
  readonly onboarding: OnboardingApi;
  readonly capture: CaptureApi;
  readonly invoices: SalesInvoiceApi;
  readonly dashboard: DashboardApi;
  readonly client: ClientApi;
  readonly customers: CustomerApi;
  readonly ledger: LedgerApi;
  readonly templates: TemplateApi;
  readonly queue: CaptureQueue;
  readonly decode: DecodeFile;
}

const ServicesContext = createContext<Services | null>(null);

export function ServicesProvider({
  overrides,
  children,
}: {
  overrides?: Partial<Services>;
  children: ReactNode;
}) {
  const { language } = useI18n();
  const { fetchImpl } = useAuth();

  const services = useMemo<Services>(() => {
    const options = { language: () => language, fetchImpl };
    const lazy = <T,>(key: keyof Services, build: () => T): T =>
      (overrides?.[key] as T | undefined) ?? build();
    return {
      account: lazy("account", () => new AccountApi(options)),
      onboarding: lazy("onboarding", () => new OnboardingApi(options)),
      capture: lazy("capture", () => new CaptureApi(options)),
      invoices: lazy("invoices", () => new SalesInvoiceApi(options)),
      dashboard: lazy("dashboard", () => new DashboardApi(options)),
      client: lazy("client", () => new ClientApi(options)),
      customers: lazy("customers", () => new CustomerApi(options)),
      ledger: lazy("ledger", () => new LedgerApi(options)),
      templates: lazy("templates", () => new TemplateApi(options)),
      // Built lazily and only when nothing was injected: `captureQueue()`
      // opens IndexedDB, which jsdom does not have.
      queue: lazy("queue", () => captureQueue()),
      decode: lazy("decode", () => browserDecode()),
    };
  }, [language, fetchImpl, overrides]);

  return <ServicesContext.Provider value={services}>{children}</ServicesContext.Provider>;
}

export function useServices(): Services {
  const value = useContext(ServicesContext);
  if (value === null) {
    throw new Error("useServices() outside a <ServicesProvider>.");
  }
  return value;
}
