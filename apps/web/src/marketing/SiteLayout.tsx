import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import {
  BookOpen,
  ChartColumn,
  ChevronDown,
  FileText,
  Landmark,
  Menu,
  Percent,
  ReceiptText,
  X,
  type LucideIcon,
} from "lucide-react";
import { SUPPORTED_LANGUAGES, useI18n } from "@ledgr/i18n";

import "./marketing.css";
import { useAuth } from "../auth/AuthProvider";
import { Logo } from "../ui/Logo";
import { ARTICLES } from "./content/articles";
import { COMMON, MODULES, type ModuleId } from "./content/common";
import { Arrow, fill } from "./blocks";
import { useL } from "./l10n";

const MODULE_ICONS: Record<ModuleId, LucideIcon> = {
  capture: ReceiptText,
  bank: Landmark,
  grootboek: BookOpen,
  btw: Percent,
  invoicing: FileText,
  reports: ChartColumn,
};

/**
 * The public site's frame (design/site, ADR-107): sticky header with the Product mega menu, the
 * EN/NL switch (the app's own language preference), log-in and "Start free", and the footer. The
 * pages render in the outlet, or as `children` when the frame wraps the home page at `/`.
 *
 * A new page starts at the top, and a link with a hash (`/product#bank`) scrolls to its section,
 * which the router does not do on its own.
 */
export function SiteLayout({ children }: { children?: ReactNode }) {
  const l = useL();
  const { language, setLanguage } = useI18n();
  const { status } = useAuth();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [megaOpen, setMegaOpen] = useState(false);
  const megaRef = useRef<HTMLLIElement>(null);

  useEffect(() => {
    setMenuOpen(false);
    setMegaOpen(false);
    if (location.hash) {
      const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (target) {
        target.scrollIntoView?.({ block: "start" });
        return;
      }
    }
    window.scrollTo(0, 0);
  }, [location.pathname, location.hash]);

  useEffect(() => {
    if (!megaOpen) return;
    const onClick = (event: MouseEvent) => {
      if (!megaRef.current?.contains(event.target as Node)) setMegaOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMegaOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [megaOpen]);

  const signedIn = status === "authenticated";
  const year = new Date().getFullYear();
  const footerArticles = ARTICLES.slice(0, 2);

  return (
    <div className="bk mk" data-testid="marketing-site">
      <a className="mk-skip" href="#mk-main">
        {l({ en: "Skip to content", nl: "Naar de inhoud" })}
      </a>
      <header className={`mk-header${menuOpen ? " mk-header--open" : ""}`}>
        <div className="mk-wrap">
          <Link className="mk-brand" to="/" aria-label={l(COMMON.brandHome)}>
            <Logo size={21} />
          </Link>
          <ul className="mk-nav" aria-label={l(COMMON.nav.label)}>
            <li ref={megaRef}>
              <button
                type="button"
                className="mk-nav-item"
                aria-expanded={megaOpen}
                aria-controls="mk-mega-product"
                onClick={() => setMegaOpen((open) => !open)}
                data-testid="mk-product-menu"
              >
                {l(COMMON.nav.product)}
                <ChevronDown size={14} strokeWidth={2} aria-hidden="true" />
              </button>
              {megaOpen ? (
                <div className="mk-mega" id="mk-mega-product">
                  {MODULES.map((module) => {
                    const Glyph = MODULE_ICONS[module.id];
                    return (
                      <Link key={module.id} to={`/product#${module.id}`}>
                        <span className="mk-ic" aria-hidden="true">
                          <Glyph size={18} strokeWidth={1.75} />
                        </span>
                        <span>
                          <b>{l(module.title)}</b>
                          <small>{l(module.blurb)}</small>
                        </span>
                      </Link>
                    );
                  })}
                  <div className="mk-mega-foot">
                    <span className="mk-muted">{l(COMMON.nav.allFeaturesHint)}</span>
                    <Link className="mk-link" to="/product">
                      {l(COMMON.nav.allFeatures)}
                      <Arrow />
                    </Link>
                  </div>
                </div>
              ) : null}
            </li>
            {(
              [
                ["/accountants", COMMON.nav.accountants],
                ["/pricing", COMMON.nav.pricing],
                ["/security", COMMON.nav.security],
                ["/articles", COMMON.nav.articles],
              ] as const
            ).map(([to, label]) => (
              <li key={to}>
                <NavLink className="mk-nav-item" to={to}>
                  {l(label)}
                </NavLink>
              </li>
            ))}
            <li>
              <Link className="mk-nav-item mk-nav-login" to={signedIn ? "/" : "/login"}>
                {l(signedIn ? COMMON.openApp : COMMON.logIn)}
              </Link>
            </li>
            {signedIn ? null : (
              <li>
                <Link className="mk-btn mk-btn--primary mk-btn--block mk-nav-start" to="/signup">
                  {l(COMMON.startFree)}
                </Link>
              </li>
            )}
          </ul>
          <div className="mk-header-actions">
            <div className="mk-lang" role="group" aria-label={l(COMMON.language)}>
              {SUPPORTED_LANGUAGES.map((option) => (
                <button
                  key={option}
                  type="button"
                  lang={option}
                  aria-pressed={option === language}
                  onClick={() => setLanguage(option)}
                  data-testid={`mk-language-${option}`}
                >
                  {option.toUpperCase()}
                </button>
              ))}
            </div>
            {signedIn ? null : (
              <Link className="mk-login" to="/login">
                {l(COMMON.logIn)}
              </Link>
            )}
            <Link
              className="mk-btn mk-btn--primary mk-btn--sm"
              to={signedIn ? "/" : "/signup"}
              data-testid="mk-start"
            >
              {l(signedIn ? COMMON.openApp : COMMON.startFree)}
            </Link>
            <button
              type="button"
              className="mk-menu-toggle"
              aria-expanded={menuOpen}
              aria-label={l(menuOpen ? COMMON.nav.closeMenu : COMMON.nav.openMenu)}
              onClick={() => setMenuOpen((open) => !open)}
            >
              {menuOpen ? (
                <X size={20} strokeWidth={2} aria-hidden="true" />
              ) : (
                <Menu size={20} strokeWidth={2} aria-hidden="true" />
              )}
            </button>
          </div>
        </div>
      </header>

      <main id="mk-main" className="mk-main" tabIndex={-1}>
        {children ?? <Outlet />}
      </main>

      <footer className="mk-footer">
        <div className="mk-wrap">
          <div className="mk-foot-grid">
            <div className="mk-foot-brand">
              <Link to="/" aria-label={l(COMMON.brandHome)}>
                <Logo size={20} />
              </Link>
              <p>{l(COMMON.footer.tagline)}</p>
            </div>
            <nav aria-labelledby="mk-foot-product">
              <h2 id="mk-foot-product">{l(COMMON.footer.product)}</h2>
              <ul>
                {MODULES.map((module) => (
                  <li key={module.id}>
                    <Link to={`/product#${module.id}`}>{l(module.title)}</Link>
                  </li>
                ))}
              </ul>
            </nav>
            <nav aria-labelledby="mk-foot-whom">
              <h2 id="mk-foot-whom">{l(COMMON.footer.forWhom)}</h2>
              <ul>
                <li>
                  <Link to="/product">{l(COMMON.footer.entrepreneurs)}</Link>
                </li>
                <li>
                  <Link to="/pricing#start">{l(COMMON.footer.zzp)}</Link>
                </li>
                <li>
                  <Link to="/pricing#grow">{l(COMMON.footer.bv)}</Link>
                </li>
                <li>
                  <Link to="/accountants">{l(COMMON.footer.accountants)}</Link>
                </li>
              </ul>
            </nav>
            <nav aria-labelledby="mk-foot-company">
              <h2 id="mk-foot-company">{l(COMMON.footer.company)}</h2>
              <ul>
                <li>
                  <Link to="/pricing">{l(COMMON.nav.pricing)}</Link>
                </li>
                <li>
                  <Link to="/security">{l(COMMON.nav.security)}</Link>
                </li>
                <li>
                  <Link to="/demo">{l(COMMON.bookDemo)}</Link>
                </li>
                <li>
                  <Link to="/contact">{l(COMMON.footer.contact)}</Link>
                </li>
              </ul>
            </nav>
            <nav aria-labelledby="mk-foot-learn">
              <h2 id="mk-foot-learn">{l(COMMON.footer.learn)}</h2>
              <ul>
                <li>
                  <Link to="/articles">{l(COMMON.footer.allArticles)}</Link>
                </li>
                {footerArticles.map((article) => (
                  <li key={article.slug}>
                    <Link to={`/articles/${article.slug}`}>
                      {l(
                        article.slug === "btw-aangifte-deadlines"
                          ? COMMON.footer.btwDeadlines
                          : COMMON.footer.bewaarplicht,
                      )}
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>
          </div>
          <div className="mk-foot-legal">
            <span>{fill(l(COMMON.footer.rights), { year })}</span>
            <nav aria-label={l({ en: "Legal", nl: "Juridisch" })}>
              <Link to="/privacy">{l(COMMON.footer.privacy)}</Link>
              <Link to="/cookies">{l(COMMON.footer.cookies)}</Link>
              <Link to="/responsible-disclosure">{l(COMMON.footer.disclosure)}</Link>
            </nav>
          </div>
        </div>
      </footer>
    </div>
  );
}
