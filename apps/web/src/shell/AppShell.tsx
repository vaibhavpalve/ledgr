import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";

import "./AppShell.css";
import { useAuth } from "../auth/AuthProvider";
import { ClientHeader } from "../client/ClientHeader";
import { useSwitcherShortcut } from "../client/ClientSwitcher";
import { useSession } from "../session/SessionProvider";
import { Wordmark } from "../Wordmark";
import { EmailVerificationBanner } from "./EmailVerificationBanner";
import { Icon, type IconName } from "./icons";
import { QuickSearch } from "./QuickSearch";
import { UserMenu } from "./UserMenu";

/**
 * The application shell — `.design/Main.dc.html` on a wide screen, the
 * five-section bottom bar below 64rem — around every authenticated route.
 *
 * --- One markup, two layouts ---
 *
 * The rail and the bottom bar are TWO navs, not one restyled: the canvas
 * gives the rail grouped headings ("Dagelijks werk", "De boeken") and a
 * settings entry pinned to its foot, none of which a five-cell bar can hold,
 * and the bar keeps the five sections ADR-046's shell settled on. CSS
 * decides which is shown (`AppShell.css`, 64rem — the same breakpoint the
 * pre-auth screen uses for its rail, ADR-057). Both carry `aria-current`
 * from `NavLink`, so the current section is announced, not only coloured.
 *
 * --- The header is the client header (FR-FRM-000a) ---
 *
 * `ClientHeader` renders the bar itself, with the fiscal-year selector, the
 * search trigger and the user menu as its children — so the client's
 * colour is the rule under the whole header, exactly as the canvas draws
 * it, and there is one header rather than a client strip inside another
 * bar. A firm at its portfolio has no active client; the header then says
 * so ("no client selected") and the rail shows only what needs no
 * administration.
 *
 * --- Focus on route change (WCAG 2.2 SC 2.4.3) ---
 *
 * The pattern `MobileShell` established for its tab switch, applied to the
 * router: a navigation replaces the whole screen, so focus moves onto
 * `<main tabIndex={-1}>` after every pathname change except the first.
 * The skip link targets the same element — "the content" is one place
 * whether you arrive by skipping the chrome or by navigating.
 */
interface NavItem {
  readonly id: string;
  readonly to: string;
  readonly labelKey: string;
  readonly icon: IconName;
  readonly end?: boolean;
}

const DAILY: readonly NavItem[] = [
  { id: "home", to: "/", labelKey: "common.nav.home", icon: "home", end: true },
  { id: "capture", to: "/capture", labelKey: "common.nav.capture", icon: "capture" },
  { id: "approve", to: "/review", labelKey: "common.nav.review", icon: "approve" },
  { id: "view", to: "/overview", labelKey: "common.nav.overview", icon: "view" },
  { id: "invoice", to: "/invoices", labelKey: "common.nav.invoices", icon: "invoice" },
  { id: "customers", to: "/customers", labelKey: "common.nav.customers", icon: "customers" },
];

const BOOKS: readonly NavItem[] = [
  { id: "ledger", to: "/ledger", labelKey: "common.nav.ledger", icon: "ledger" },
];

const FIRM: readonly NavItem[] = [
  { id: "clients", to: "/clients", labelKey: "common.nav.clients", icon: "clients" },
];

/** The bottom bar's five: ADR-046's four tasks plus FR-UX-005's home, unchanged. */
const COMPACT_TABS: readonly NavItem[] = DAILY.slice(0, 5);

export function AppShell() {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const { requestSignOut } = useAuth();
  const { me, administration, badge, fiscalYear, fiscalYears, selectFiscalYear } = useSession();
  const [searchOpen, setSearchOpen] = useState(false);
  const openSearch = useCallback(() => setSearchOpen(true), []);
  useSwitcherShortcut(openSearch);

  const mainRef = useRef<HTMLElement>(null);
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    mainRef.current?.focus();
  }, [location.pathname]);

  const isFirm = me.organization.kind === "firm";
  const hasAdministration = administration !== null;
  const rail = [
    ...(hasAdministration ? [{ heading: t("common.nav.group.daily"), items: DAILY }] : []),
    ...(hasAdministration ? [{ heading: t("common.nav.group.books"), items: BOOKS }] : []),
    ...(isFirm ? [{ heading: t("common.nav.group.firm"), items: FIRM }] : []),
  ];
  const compactExtras = [
    ...(hasAdministration
      ? [
          { to: "/customers", label: t("common.nav.customers") },
          { to: "/ledger", label: t("common.nav.ledger") },
        ]
      : []),
    ...(isFirm ? [{ to: "/clients", label: t("common.nav.clients") }] : []),
  ];

  return (
    <div className="shell" data-testid="app-shell" data-layout={hasAdministration ? "administration" : "portfolio"}>
      {/*
        WCAG 2.2 SC 2.4.1. First in the DOM so it is the first thing Tab
        reaches, and invisible until focused (see .ledgr-skip-link in
        tokens.css).
      */}
      <a className="ledgr-skip-link" href="#main-content" data-testid="skip-to-content">
        {t("common.skip_to_content")}
      </a>

      <aside className="shell__rail" data-testid="shell-rail">
        <div className="shell__brand">
          <Wordmark />
        </div>
        <nav className="shell__nav" aria-label={t("common.nav.label")}>
          {rail.map((group) => (
            <div key={group.heading} className="shell__nav-group">
              <p className="label shell__nav-heading">{group.heading}</p>
              {group.items.map((item) => (
                <NavLink
                  key={item.id}
                  to={item.to}
                  end={item.end}
                  className="shell__nav-link"
                  data-testid={`nav-${item.id}`}
                >
                  <Icon name={item.icon} />
                  <span>{t(item.labelKey)}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="shell__rail-foot">
          <NavLink to="/settings" className="shell__nav-link" data-testid="nav-settings">
            <Icon name="settings" />
            <span>{t("common.nav.settings")}</span>
          </NavLink>
        </div>
      </aside>

      <ClientHeader badge={badge}>
        <span className="shell__header-brand">
          <Wordmark />
        </span>
        {isFirm ? (
          <button
            type="button"
            className="button--quiet shell__switch"
            data-testid="shell-switch-client"
            onClick={() => navigate("/clients")}
          >
            <span>{hasAdministration ? t("client.switcher.dialog_label") : t("client.portfolio.choose")}</span>
            <Icon name="chevron-down" size={18} />
          </button>
        ) : null}
        {hasAdministration && fiscalYears.length > 0 ? (
          <label className="shell__fiscal-year">
            <span className="ledgr-visually-hidden">{t("common.fiscal_year.label")}</span>
            <select
              value={fiscalYear?.id ?? ""}
              data-testid="fiscal-year-select"
              onChange={(event) => selectFiscalYear(event.target.value)}
            >
              {fiscalYears.map((year) => (
                <option key={year.id} value={year.id}>
                  {t("common.fiscal_year.option", { label: fiscalYearLabel(year.start_date, year.end_date) })}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <button
          type="button"
          className="shell__search"
          data-testid="shell-search"
          onClick={openSearch}
        >
          <Icon name="search" size={18} />
          <span className="shell__search-text">{t("common.search.placeholder")}</span>
          <kbd className="shell__kbd" aria-hidden="true">
            {t("common.search.shortcut")}
          </kbd>
        </button>
        <div className="shell__header-spacer" />
        <UserMenu email={me.user.email} onSignOut={requestSignOut} extraLinks={compactExtras} />
      </ClientHeader>

      <main
        ref={mainRef}
        id="main-content"
        className="shell__main"
        tabIndex={-1}
        data-testid="mobile-shell-content"
      >
        {!me.user.email_verified ? <EmailVerificationBanner email={me.user.email} /> : null}
        <Outlet />
      </main>

      {hasAdministration ? (
        <nav aria-label={t("mobile.shell.nav_label")} className="shell__tabs" data-testid="mobile-shell-tabs">
          {COMPACT_TABS.map((item) => (
            <NavLink
              key={item.id}
              to={item.to}
              end={item.end}
              className="shell__tab"
              data-testid={`mobile-tab-${item.id}`}
            >
              <span className="shell__tab-icon" aria-hidden="true">
                <Icon name={item.icon} />
              </span>
              <span>{t(item.labelKey)}</span>
            </NavLink>
          ))}
        </nav>
      ) : null}

      <QuickSearch open={searchOpen} onClose={() => setSearchOpen(false)} />
    </div>
  );
}

/**
 * "2026" for a calendar year, "2025/2026" for a broken one — the label the
 * canvas's "Boekjaar 2026" uses. Years only: the selector is a choice among
 * a few, and the full dates are on the settings screen.
 */
export function fiscalYearLabel(startDate: string, endDate: string): string {
  const startYear = startDate.slice(0, 4);
  const endYear = endDate.slice(0, 4);
  return startYear === endYear ? startYear : `${startYear}/${endYear}`;
}
