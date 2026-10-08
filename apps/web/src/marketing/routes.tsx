import type { ReactElement } from "react";

import { SiteLayout } from "./SiteLayout";
import {
  AccountantsPage,
  ArticlePage,
  ArticlesPage,
  DemoPage,
  HomePage,
  LegalPage,
  PricingPage,
  ProductPage,
  SecurityPage,
  SiteNotFound,
} from "./pages";

/**
 * The public site's entry point, the one module `App.tsx` loads lazily (ADR-107): the layout, the
 * pages by name, and the home page in its frame for `/`.
 */
export { SiteLayout };

const PAGES: Record<string, () => ReactElement> = {
  ProductPage: () => <ProductPage />,
  AccountantsPage: () => <AccountantsPage />,
  PricingPage: () => <PricingPage />,
  SecurityPage: () => <SecurityPage />,
  DemoPage: () => <DemoPage />,
  ArticlesPage: () => <ArticlesPage />,
  ArticlePage: () => <ArticlePage />,
  PrivacyPage: () => <LegalPage kind="privacy" />,
  CookiesPage: () => <LegalPage kind="cookies" />,
  DisclosurePage: () => <LegalPage kind="disclosure" />,
};

export function SitePageByName({ name }: { name: string }) {
  const render = PAGES[name];
  return render ? render() : <SiteNotFound />;
}

export function SiteHome() {
  return (
    <SiteLayout>
      <HomePage />
    </SiteLayout>
  );
}
