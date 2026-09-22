import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  BookOpen,
  Building,
  ChartColumn,
  ChevronDown,
  FileText,
  House,
  Landmark,
  LayoutGrid,
  LogOut,
  Search,
  Settings,
  ScrollText,
  ShoppingCart,
  Users,
  type LucideIcon,
} from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { ClientBadge } from "@ledgr/shared-types";

import "./AppShell.css";
import "./Shell.css";
import { useAuth } from "../auth/AuthProvider";
import { ClientHeader } from "../client/ClientHeader";
import { useSwitcherShortcut } from "../client/ClientSwitcher";
import { useServices } from "../session/ServicesProvider";
import { useSession } from "../session/SessionProvider";
import { Badge, Logo, NavGroup, NavItem } from "../ui";
import { EmailVerificationBanner } from "./EmailVerificationBanner";
import { QuickSearch } from "./QuickSearch";
import { UserMenu } from "./UserMenu";

/**
 * The application shell: design/reference/Home.png's sidebar and header on a
 * wide screen, the five-section bottom bar below 64rem, around every
 * authenticated route.
 *
 * --- One markup, two layouts ---
 *
 * The rail and the bottom bar are TWO navs, not one restyled: the rail has
 * grouped headings, the company card and Settings / Sign out pinned to its
 * foot, none of which a five-cell bar can hold. CSS decides which is shown
 * (`Shell.css`, 64rem, the same breakpoint the pre-auth screen uses). Both carry
 * `aria-current` from `NavLink`, so the current section is announced, not only
 * coloured.
 *
 * --- The active client stays unmistakable (FR-FRM-000a) ---
 *
 * `ClientHeader` still renders the header bar, so the client's colour is still
 * the rule under it and its name is still the first thing a screen reader
 * meets. On a wide screen the handoff moves the visible identity to the
 * company card at the top of the rail (marker in the client's own colour,
 * initials, name, KvK); on a phone, where the rail is gone, the header shows
 * marker and name itself. See ADR-080.
 *
 * --- Focus on route change (WCAG 2.2 SC 2.4.3) ---
 *
 * A navigation replaces the whole screen, so focus moves onto
 * `<main tabIndex={-1}>` after every pathname change except the first. The skip
 * link targets the same element.
 */
interface NavDef {
  readonly id: string;
  readonly to: string;
  readonly labelKey: string;
  readonly icon: LucideIcon;
  readonly end?: boolean;
}

const HOME: NavDef = { id: "home", to: "/", labelKey: "common.nav.home", icon: House, end: true };
const INVOICES: NavDef = {
  id: "invoice",
  to: "/invoices",
  labelKey: "common.nav.invoices",
  icon: FileText,
};
const CLIENTS: NavDef = {
  id: "clients",
  to: "/clients",
  labelKey: "common.nav.clients",
  icon: Users,
};

/**
 * The rail's nine sections, in the order of the Boekje design. Their ids are the
 * ones the rail has always used (`nav-home`, `nav-invoice`, ...) so nothing that
 * finds an item by test id changes; only what it is called and where it sits.
 *
 * "Purchases" is ONE item: uploading is at the top of its screen and reviewing is
 * inside each invoice, so Capture, Review and the old Overview no longer have
 * menu entries of their own (their addresses redirect there).
 */
const DASHBOARD: NavDef = {
  id: "home",
  to: "/",
  labelKey: "common.nav.dashboard",
  icon: LayoutGrid,
  end: true,
};
const SALES: NavDef = {
  id: "invoice",
  to: "/invoices",
  labelKey: "common.nav.sales",
  icon: FileText,
};
const PURCHASES: NavDef = {
  id: "purchases",
  to: "/purchases",
  labelKey: "common.nav.purchases",
  icon: ShoppingCart,
};
const BANK: NavDef = { id: "bank", to: "/bank", labelKey: "common.nav.bank", icon: Landmark };
const JOURNAL: NavDef = {
  id: "journal",
  to: "/journal",
  labelKey: "common.nav.journal",
  icon: ScrollText,
};
const LEDGER: NavDef = {
  id: "ledger",
  to: "/ledger",
  labelKey: "common.nav.ledger",
  icon: BookOpen,
};
const ASSETS: NavDef = {
  id: "assets",
  to: "/assets",
  labelKey: "common.nav.assets",
  icon: Building,
};
const CONTACTS: NavDef = {
  id: "customers",
  to: "/customers",
  labelKey: "common.nav.contacts",
  icon: Users,
};
const REPORTS: NavDef = {
  id: "reports",
  to: "/reports",
  labelKey: "common.nav.reports",
  icon: ChartColumn,
};

/** The bottom bar: home, and the three places most of a day is spent (FR-UX-005). */
const COMPACT_TABS: readonly NavDef[] = [HOME, PURCHASES, INVOICES, LEDGER];

export function AppShell() {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const { requestSignOut } = useAuth();
  const { me, administration, badge, fiscalYear, fiscalYears, selectFiscalYear } = useSession();
  const [searchOpen, setSearchOpen] = useState(false);
  const openSearch = useCallback(() => setSearchOpen(true), []);
  useSwitcherShortcut(openSearch);

  // The count beside "Purchases": invoices still To review, from the same
  // dashboard call Home makes. Secondary chrome, so a failure just shows no badge.
  const { dashboard } = useServices();
  const administrationId = administration?.id ?? null;
  const fiscalYearId = fiscalYear?.id ?? null;
  const [reviewCount, setReviewCount] = useState<number | null>(null);
  useEffect(() => {
    if (administrationId === null || fiscalYearId === null) {
      setReviewCount(null);
      return;
    }
    let cancelled = false;
    void Promise.resolve()
      .then(() => dashboard.getDashboard(administrationId, fiscalYearId))
      .then((summary) => {
        if (!cancelled) setReviewCount(summary.receipts_to_review ?? null);
      })
      .catch(() => {
        if (!cancelled) setReviewCount(null);
      });
    return () => {
      cancelled = true;
    };
  }, [dashboard, administrationId, fiscalYearId]);

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
  const reviewBadge =
    reviewCount !== null && reviewCount > 0 ? (
      <Badge variant="review" data-testid="nav-review-count">
        {reviewCount}
      </Badge>
    ) : undefined;
  const navItem = (item: NavDef, badge?: ReactNode) => (
    <NavItem
      key={item.id}
      to={item.to}
      end={item.end}
      icon={item.icon}
      testId={`nav-${item.id}`}
      badge={badge}
    >
      {t(item.labelKey)}
    </NavItem>
  );
  const initial = (me.user.email.trim()[0] ?? "?").toUpperCase();
  // Below 64rem the rail is gone and the bottom bar holds four, so everything
  // else the rail lists is reached from the avatar menu.
  const compactExtras = [
    ...(hasAdministration
      ? [
          { to: "/invoices", label: t("common.nav.sales") },
          { to: "/customers", label: t("common.nav.contacts") },
          { to: "/ledger", label: t("common.nav.ledger") },
          { to: "/bank", label: t("common.nav.bank") },
          { to: "/journal", label: t("common.nav.journal") },
          { to: "/assets", label: t("common.nav.assets") },
          { to: "/reports", label: t("common.nav.reports") },
        ]
      : []),
    ...(isFirm ? [{ to: "/clients", label: t("common.nav.clients") }] : []),
  ];

  return (
    <div
      className="shell ui-root"
      data-testid="app-shell"
      data-layout={hasAdministration ? "administration" : "portfolio"}
    >
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
          <Logo />
        </div>

        <CompanyCard badge={badge} switchable={isFirm} onSwitch={() => navigate("/clients")} />

        <nav className="shell__nav" aria-label={t("common.nav.label")}>
          {hasAdministration ? (
            <div className="shell__nav-group">
              <NavGroup>{t("common.nav.group.bookkeeping")}</NavGroup>
              {navItem(DASHBOARD)}
              {navItem(SALES)}
              {navItem(PURCHASES, reviewBadge)}
              {navItem(BANK)}
              {navItem(JOURNAL)}
              {navItem(LEDGER)}
              {navItem(ASSETS)}
              {navItem(CONTACTS)}
              {navItem(REPORTS)}
            </div>
          ) : null}
          {isFirm ? (
            <div className="shell__nav-group">
              <NavGroup>{t("common.nav.group.firm")}</NavGroup>
              {navItem(CLIENTS)}
            </div>
          ) : null}
        </nav>

        <div className="shell__rail-foot">
          <NavItem to="/settings" icon={Settings} testId="nav-settings">
            {t("common.nav.settings")}
          </NavItem>
          {/* Who is signed in, at the foot of the rail. Sign out sits inside it
              as an icon: the design has no separate row for it, and it stays one
              click from here as before. */}
          <div className="shell__user" data-testid="shell-user">
            <span className="shell__avatar" aria-hidden="true">
              {initial}
            </span>
            <span className="shell__user-text">
              <span className="shell__user-name">{me.user.email}</span>
              <span className="shell__user-role">
                {isFirm ? t("common.nav.role.firm") : t("common.nav.role.owner")}
              </span>
            </span>
            <button
              type="button"
              className="shell__signout"
              aria-label={t("auth.sign_out")}
              title={t("auth.sign_out")}
              data-testid="nav-sign-out"
              onClick={requestSignOut}
            >
              <LogOut size={16} strokeWidth={1.8} aria-hidden="true" />
            </button>
          </div>
        </div>
      </aside>

      <ClientHeader badge={badge}>
        <span className="shell__header-brand">
          <Logo size={24} />
        </span>
        {isFirm ? (
          <button
            type="button"
            className="ui-textbutton shell__switch"
            data-testid="shell-switch-client"
            onClick={() => navigate("/clients")}
          >
            <span>
              {hasAdministration ? t("client.switcher.dialog_label") : t("client.portfolio.choose")}
            </span>
            <ChevronDown size={16} strokeWidth={1.8} aria-hidden="true" />
          </button>
        ) : null}
        <button
          type="button"
          className="shell__search"
          data-testid="shell-search"
          onClick={openSearch}
        >
          <Search size={18} strokeWidth={1.8} aria-hidden="true" />
          <span className="shell__search-text">{t("common.search.placeholder")}</span>
          <kbd className="shell__kbd" aria-hidden="true">
            {t("common.search.shortcut")}
          </kbd>
        </button>
        <div className="shell__header-spacer" />
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
                  {t("common.fiscal_year.option", {
                    label: fiscalYearLabel(year.start_date, year.end_date),
                  })}
                </option>
              ))}
            </select>
            <ChevronDown
              className="shell__fiscal-year-chevron"
              size={14}
              strokeWidth={2}
              aria-hidden="true"
            />
          </label>
        ) : null}
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
        <nav
          aria-label={t("mobile.shell.nav_label")}
          className="shell__tabs"
          data-testid="mobile-shell-tabs"
        >
          {COMPACT_TABS.map((item) => (
            <NavLink
              key={item.id}
              to={item.to}
              end={item.end}
              className="shell__tab"
              data-testid={`mobile-tab-${item.id}`}
            >
              <span className="shell__tab-icon" aria-hidden="true">
                <item.icon size={20} strokeWidth={1.7} />
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
 * The company switcher card at the top of the rail (design DESIGN.md 6.2): the
 * active client's marker, name and KvK number. The marker keeps the client's
 * own colour and initials (FR-FRM-000a), so it is the same signal the header
 * rule gives. For a firm the whole card is the way to the portfolio.
 *
 * `undefined` badge is "not known yet" and `null` is "confirmed: no client",
 * exactly as `ClientHeader` treats them; neither may read as an open client.
 */
function CompanyCard({
  badge,
  switchable,
  onSwitch,
}: {
  badge: ClientBadge | null | undefined;
  switchable: boolean;
  onSwitch: () => void;
}) {
  const { t } = useI18n();

  if (badge === undefined) {
    return (
      <div className="shell__company" role="status">
        <span className="shell__company-name">{t("client.header.loading")}</span>
      </div>
    );
  }

  const body =
    badge === null ? (
      <span className="shell__company-text">
        <span className="shell__company-name">{t("client.header.none_selected")}</span>
      </span>
    ) : (
      <>
        <span
          className={`client-marker client-marker--${badge.colour} shell__company-marker`}
          aria-hidden="true"
        >
          {badge.initials}
        </span>
        <span className="shell__company-text">
          <span className="shell__company-name">{badge.displayName}</span>
          {badge.kvkNumber !== null ? (
            <span className="shell__company-kvk">
              {t("client.switcher.kvk", { number: badge.kvkNumber })}
            </span>
          ) : null}
        </span>
      </>
    );

  if (!switchable) {
    return (
      <div className="shell__company" data-testid="shell-company">
        {body}
      </div>
    );
  }

  return (
    <button
      type="button"
      className="shell__company shell__company--button"
      data-testid="shell-company"
      onClick={onSwitch}
    >
      {body}
      <ChevronDown size={16} strokeWidth={2} aria-hidden="true" />
    </button>
  );
}

/**
 * "2026" for a calendar year, "2025/2026" for a broken one. Years only: the
 * selector is a choice among a few, and the full dates are on the settings
 * screen.
 */
export function fiscalYearLabel(startDate: string, endDate: string): string {
  const startYear = startDate.slice(0, 4);
  const endYear = endDate.slice(0, 4);
  return startYear === endYear ? startYear : `${startYear}/${endYear}`;
}
