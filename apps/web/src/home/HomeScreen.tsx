import { useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type { DashboardActionItemView, DashboardView } from "@ledgr/shared-types";

import "./HomeScreen.css";
import type { MobileTab } from "../MobileShell";
import type { DashboardApi } from "./api";

/**
 * FR-UX-005 / MOB-006: the home screen.
 *
 *     FR-UX-005  The home screen is a prioritised list of what needs the
 *                user's attention, not a menu of everything the product can do.
 *     MOB-006    Dashboard: cash position, receivables, VAT estimate, items
 *                needing attention.
 *
 * --- What changed, and why (ADR-055) ---
 *
 * This screen used to open with the three figures and put the prioritised list
 * underneath them. That is the dashboard pattern every bookkeeping product
 * shipped a decade ago, and it is backwards for this one: all three figures
 * are LAGGING indicators that nobody can act on, while the list below them is
 * the entire reason FR-UX-005 exists.
 *
 * So the order is inverted. The attention list is the hero — first in the DOM,
 * first in the reading order, first under a screen reader — and the figures
 * follow it as a compact row of context. Same data, same honesty captions,
 * opposite emphasis.
 *
 * A genuinely deadline-led strip ("BTW due in 9 days, 4 documents unposted")
 * would be better still, and is deliberately NOT built here: the days-until-
 * filing figure is not on `DashboardView`, and deriving it in the client would
 * put statutory business logic in the browser, which this product does not do.
 * It needs an API field first.
 *
 * The API has already ordered the list (FR-UX-005's ranking is the backend's
 * job, tested there) and this component does not re-sort it.
 *
 * Tapping an item calls `onNavigate` with the tab that item belongs to.
 * Neither `ApproveList` nor `ViewList` supports opening directly to one
 * record today, so a tap switches tabs only - see this feature's ADR, Known
 * gaps - it does not jump straight to the item.
 */
export function HomeScreen({
  administrationId,
  fiscalYearId,
  api,
  onNavigate,
}: {
  administrationId: string;
  fiscalYearId: string;
  api: DashboardApi;
  onNavigate: (tab: MobileTab) => void;
}) {
  const { t, money, date } = useI18n();
  const [summary, setSummary] = useState<DashboardView | null>(null);
  const [problem, setProblem] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setSummary(null);
    setProblem(false);
    api
      .getDashboard(administrationId, fiscalYearId)
      .then((result) => {
        if (!cancelled) setSummary(result);
      })
      .catch(() => {
        if (!cancelled) setProblem(true);
      });
    return () => {
      cancelled = true;
    };
  }, [administrationId, fiscalYearId, api]);

  return (
    <section className="home" aria-label={t("mobile.home.title")}>
      <h1>{t("mobile.home.title")}</h1>

      {problem ? (
        <p role="alert" data-testid="home-error" className="alert alert--attention">
          {t("mobile.common.error")}
        </p>
      ) : null}

      {/*
        A skeleton rather than a line of text, so the page does not jump when
        the data lands. `role="status"` keeps the announcement that the old
        "Loading…" paragraph carried — the visual treatment changed, the
        accessible one did not — and the visible text stays in the tree for
        anyone who needs it, hidden from sight only.
      */}
      {summary === null && !problem ? (
        <div role="status" data-testid="home-loading" className="home__loading">
          <span className="ledgr-visually-hidden">{t("mobile.common.loading")}</span>
          <div className="skeleton home__loading-row" />
          <div className="skeleton home__loading-row" />
          <div className="skeleton home__loading-row" />
        </div>
      ) : null}

      {summary !== null ? (
        <>
          {/*
            FIRST: the only part of this screen anyone can act on.
          */}
          <section className="home__section" aria-label={t("mobile.home.items_heading")}>
            <div className="home__section-head">
              <h2>{t("mobile.home.items_heading")}</h2>
              {summary.items_needing_action.length > 0 ? (
                <span className="chip chip--attention" data-testid="home-item-count">
                  {t("mobile.home.item_count", { count: summary.items_needing_action.length })}
                </span>
              ) : null}
            </div>

            {summary.items_needing_action.length === 0 ? (
              <div className="panel empty-state" data-testid="home-items-empty">
                <span className="empty-state__icon" aria-hidden="true">
                  <svg
                    width="32"
                    height="32"
                    viewBox="0 0 20 20"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <circle cx="10" cy="10" r="7.3" />
                    <path d="m6.6 10.2 2.4 2.4 4.5-5" />
                  </svg>
                </span>
                <p className="empty-state__title">{t("mobile.home.items_empty_title")}</p>
                <p className="empty-state__body">{t("mobile.home.items_empty")}</p>
              </div>
            ) : (
              <ul
                className="panel list"
                aria-label={t("mobile.home.items_heading")}
                data-testid="home-items-list"
              >
                {summary.items_needing_action.map((item) => (
                  <li key={item.id} data-testid="home-item">
                    <button
                      type="button"
                      className="list__row"
                      data-testid={`home-item-${item.id}`}
                      onClick={() => onNavigate(tabFor(item))}
                    >
                      <span className={`home__item-icon home__item-icon--${item.kind}`}>
                        <ItemIcon kind={item.kind} />
                      </span>
                      <span className="list__row-text">
                        {/* Already translated server-side (FR-UX-007) — placed
                            as-is rather than recomposed from `kind`. */}
                        <span>{item.description}</span>
                      </span>
                      <span className="home__item-chevron" aria-hidden="true">
                        <svg
                          width="18"
                          height="18"
                          viewBox="0 0 20 20"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.67"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <path d="m8 5.5 4.5 4.5L8 14.5" />
                        </svg>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/*
            THEN: context. Same three figures, same honesty captions, a third
            of the height and no longer the first thing the eye lands on.

            WCAG 2.2 / SC 1.3.1 (Info and Relationships): a `<dl>` may only
            directly contain <dt>/<dd> groups (each optionally wrapped in a
            <div>, which is what groups one term with its value here) — axe
            (this file's own CMP-012/FR-LOC-004 coverage in HomeScreen.test.tsx,
            via MobileShell.test.tsx's Home-tab check) flagged the caption
            below as a stray <p> inside that div, which breaks the group. A
            <dt> may have more than one <dd>, so the caption is a second
            description of the same term rather than an unrelated paragraph —
            not a workaround, the correct reading of what a caption under a
            figure actually is.
          */}
          <section className="home__section" aria-label={t("mobile.home.figures_heading")}>
            <div className="home__section-head">
              <h2>{t("mobile.home.figures_heading")}</h2>
            </div>

            <dl className="home__figures" data-testid="home-figures">
              <div className="home__figure">
                <dt className="label">{t("mobile.home.cash_position_label")}</dt>
                <dd className="ledgr-figure home__figure-value" data-testid="home-cash-position">
                  {money(summary.cash_position)}
                </dd>
                <dd className="caption" data-testid="home-cash-position-caption">
                  {t("mobile.home.cash_position_caption")}
                </dd>
              </div>

              <div className="home__figure">
                <dt className="label">{t("mobile.home.receivables_label")}</dt>
                <dd className="ledgr-figure home__figure-value" data-testid="home-receivables">
                  {money(summary.receivables)}
                </dd>
                <dd className="caption" data-testid="home-receivables-caption">
                  {t("mobile.home.receivables_caption")}
                </dd>
              </div>

              <div className="home__figure">
                <dt className="label">{t("mobile.home.vat_estimate_label")}</dt>
                <dd className="ledgr-figure home__figure-value" data-testid="home-vat-estimate">
                  {money(summary.vat_estimate)}
                </dd>
                <dd className="caption" data-testid="home-vat-estimate-period">
                  {t("mobile.home.vat_estimate_period", {
                    start: date(summary.vat_period_start),
                    end: date(summary.vat_period_end),
                  })}
                </dd>
                <dd className="caption" data-testid="home-vat-estimate-caption">
                  {t("mobile.home.vat_estimate_caption")}
                </dd>
              </div>
            </dl>
          </section>
        </>
      ) : null}
    </section>
  );
}

/**
 * Which existing tab a tapped item routes to. Neither destination opens
 * directly to the specific record (see this component's own docstring and
 * this feature's ADR) - the tab is the closest existing screen that already
 * lists the item, per kind:
 *
 *   overdue_invoice  the invoice is issued - View's invoices list has it.
 *   draft_invoice    View's invoices list includes drafts too (MOB-005).
 *   draft_expense    Approve is exactly "draft expenses awaiting completion".
 */
function tabFor(item: DashboardActionItemView): MobileTab {
  return item.kind === "draft_expense" ? "approve" : "view";
}

/**
 * An icon per item kind.
 *
 * The colour is applied by the wrapper's `--{kind}` class in HomeScreen.css,
 * not here, and only `overdue_invoice` gets the attention colour: that is the
 * one kind where something has actually gone wrong. A draft is not a problem,
 * it is unfinished work, and colouring it as an alarm is how an alarm stops
 * meaning anything.
 */
function ItemIcon({ kind }: { kind: DashboardActionItemView["kind"] }) {
  const shared = {
    width: 18,
    height: 18,
    viewBox: "0 0 20 20",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.67,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
  } as const;

  if (kind === "overdue_invoice") {
    // A clock: the thing that is wrong is elapsed time, not the document.
    return (
      <svg {...shared}>
        <circle cx="10" cy="10" r="7.3" />
        <path d="M10 5.8v4.4l3 1.8" />
      </svg>
    );
  }

  // Both draft kinds: a document with a line missing from it.
  return (
    <svg {...shared}>
      <path d="M5 2.8h6l4 4v10.4a.8.8 0 0 1-.8.8H5a.8.8 0 0 1-.8-.8V3.6a.8.8 0 0 1 .8-.8z" />
      <path d="M11 2.8v4h4" />
      <path d="M7.4 12.4h3" />
    </svg>
  );
}
