import { useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type { DashboardActionItemView, DashboardView } from "@ledgr/shared-types";

import type { MobileTab } from "../MobileShell";
import type { DashboardApi } from "./api";

/**
 * FR-UX-005 / MOB-006: the mobile home screen.
 *
 *     FR-UX-005  The home screen is a prioritised list of what needs the
 *                user's attention, not a menu of everything the product can do.
 *     MOB-006    Dashboard: cash position, receivables, VAT estimate, items
 *                needing attention.
 *
 * Three summary figures, each with an honesty caption (see api.dashboard.model
 * and this feature's ADR for exactly what each does and does not include),
 * plus the prioritised list the API already ordered - this component does not
 * re-sort it (FR-UX-005's ranking is the backend's job, tested there).
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
    <section aria-label={t("mobile.home.title")}>
      <h1>{t("mobile.home.title")}</h1>

      {problem ? (
        <p role="alert" data-testid="home-error">
          {t("mobile.common.error")}
        </p>
      ) : null}

      {summary === null && !problem ? (
        <p role="status" data-testid="home-loading">
          {t("mobile.common.loading")}
        </p>
      ) : null}

      {summary !== null ? (
        <>
          <dl data-testid="home-figures">
            <div>
              <dt>{t("mobile.home.cash_position_label")}</dt>
              <dd data-testid="home-cash-position">{money(summary.cash_position)}</dd>
              <p data-testid="home-cash-position-caption">
                {t("mobile.home.cash_position_caption")}
              </p>
            </div>

            <div>
              <dt>{t("mobile.home.receivables_label")}</dt>
              <dd data-testid="home-receivables">{money(summary.receivables)}</dd>
              <p data-testid="home-receivables-caption">{t("mobile.home.receivables_caption")}</p>
            </div>

            <div>
              <dt>{t("mobile.home.vat_estimate_label")}</dt>
              <dd data-testid="home-vat-estimate">{money(summary.vat_estimate)}</dd>
              <p data-testid="home-vat-estimate-period">
                {t("mobile.home.vat_estimate_period", {
                  start: date(summary.vat_period_start),
                  end: date(summary.vat_period_end),
                })}
              </p>
              <p data-testid="home-vat-estimate-caption">{t("mobile.home.vat_estimate_caption")}</p>
            </div>
          </dl>

          <section aria-label={t("mobile.home.items_heading")}>
            <h2>{t("mobile.home.items_heading")}</h2>

            {summary.items_needing_action.length === 0 ? (
              <p data-testid="home-items-empty">{t("mobile.home.items_empty")}</p>
            ) : (
              <ul aria-label={t("mobile.home.items_heading")} data-testid="home-items-list">
                {summary.items_needing_action.map((item) => (
                  <li key={item.id} data-testid="home-item">
                    <button
                      type="button"
                      data-testid={`home-item-${item.id}`}
                      onClick={() => onNavigate(tabFor(item))}
                    >
                      {item.description}
                    </button>
                  </li>
                ))}
              </ul>
            )}
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
