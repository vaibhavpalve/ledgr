import { useCallback, useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type { ExpenseSummaryView, ExpenseView } from "@ledgr/shared-types";

import type { CaptureApi } from "../capture/api";
import { ExpenseForm } from "../capture/ExpenseForm";

/**
 * MOB-004's Approve tab.
 *
 * "Approve" here is the scope decision this task's ADR records explicitly: a
 * list of the administration's DRAFT expenses (captured but not yet
 * reviewed/completed), tappable into the EXISTING `ExpenseForm`/
 * `mark_expense_ready` flow (untouched below) — not a new multi-actor
 * approval workflow, which FR-EXP-002/FR-AP-006 defer to P1.
 *
 * A completed item drops off this list because it stops being `draft` the
 * moment `ExpenseForm`'s "mark ready" succeeds — the list is re-fetched
 * rather than locally filtered, so it reflects the server's own idea of what
 * is still outstanding.
 */
export function ApproveList({
  administrationId,
  api,
  openId: openIdProp,
  onOpenChange,
}: {
  administrationId: string;
  api: CaptureApi;
  /**
   * Which draft is open — controlled by the route (`/review/:expenseId`)
   * when given, so a dashboard action or a deep link lands straight on one
   * receipt's form; the list's own state otherwise, exactly as before.
   */
  openId?: string | null;
  onOpenChange?: (id: string | null) => void;
}) {
  const { t, money, date } = useI18n();
  const [items, setItems] = useState<readonly ExpenseSummaryView[] | null>(null);
  const [problem, setProblem] = useState(false);
  const [openIdState, setOpenIdState] = useState<string | null>(null);
  const openId = openIdProp === undefined ? openIdState : openIdProp;
  const setOpenId = (id: string | null) => {
    setOpenIdState(id);
    onOpenChange?.(id);
  };

  const load = useCallback(() => {
    setProblem(false);
    setItems(null);
    api
      .listExpenses(administrationId, "draft")
      .then((result) => setItems(result))
      .catch(() => setProblem(true));
  }, [administrationId, api]);

  useEffect(() => {
    load();
  }, [load]);

  if (openId !== null) {
    return (
      <section aria-label={t("mobile.approve.title")} className="screen">
        <button
          type="button"
          className="button--quiet"
          data-testid="approve-back"
          onClick={() => {
            setOpenId(null);
            load();
          }}
        >
          {t("mobile.approve.back")}
        </button>
        <ExpenseFormLoader
          administrationId={administrationId}
          expenseId={openId}
          api={api}
          onReady={() => {
            setOpenId(null);
            load();
          }}
        />
      </section>
    );
  }

  return (
    <section aria-label={t("mobile.approve.title")} className="screen">
      <h1>{t("mobile.approve.title")}</h1>

      {items === null && !problem ? (
        <div role="status" data-testid="approve-loading" className="skeleton-list">
          <span className="ledgr-visually-hidden">{t("mobile.common.loading")}</span>
          <div className="skeleton skeleton-list__row" aria-hidden="true" />
          <div className="skeleton skeleton-list__row" aria-hidden="true" />
        </div>
      ) : null}

      {problem ? (
        <div
          role="alert"
          data-testid="approve-error"
          className="alert alert--attention error-state"
        >
          <div className="error-state__text">
            <p>{t("mobile.common.error")}</p>
            <p className="error-state__hint">{t("common.error.next_step")}</p>
          </div>
          <button type="button" onClick={load}>
            {t("common.action.retry")}
          </button>
        </div>
      ) : null}

      {items !== null && items.length === 0 ? (
        <div className="panel empty-state" data-testid="approve-empty">
          <p className="empty-state__title">{t("mobile.approve.empty")}</p>
          <p className="empty-state__body">{t("mobile.approve.empty_hint")}</p>
        </div>
      ) : null}

      {items !== null && items.length > 0 ? (
        <ul
          className="panel list"
          aria-label={t("mobile.approve.list_label")}
          data-testid="approve-list"
        >
          {items.map((item) => (
            <li key={item.id} data-testid="approve-item">
              <button
                type="button"
                className="list__row"
                data-testid={`approve-open-${item.id}`}
                onClick={() => setOpenId(item.id)}
              >
                <span className="list__row-text">
                  <span data-testid="approve-item-supplier">
                    {item.supplier ?? t("mobile.approve.untitled")}
                  </span>
                  {item.expense_date !== null ? (
                    <span className="caption ledgr-num">{date(item.expense_date)}</span>
                  ) : null}
                </span>
                {item.gross_amount !== null ? (
                  <span className="list__row-amount">{money(item.gross_amount)}</span>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/**
 * Fetches the full form view for one expense and hands it to `ExpenseForm`
 * unchanged. The list above only has `ExpenseSummaryView` — enough to render
 * a row, not enough to draw the form — so opening an item is a real
 * `GET .../expenses/{id}` the same way `ExpenseForm`'s own PATCH/ready calls
 * already are.
 */
function ExpenseFormLoader({
  administrationId,
  expenseId,
  api,
  onReady,
}: {
  administrationId: string;
  expenseId: string;
  api: CaptureApi;
  /** Called once the expense leaves `draft` (FR-EXP-001b's "mark ready"). */
  onReady: () => void;
}) {
  const { t } = useI18n();
  const [expense, setExpense] = useState<ExpenseView | null>(null);
  const [problem, setProblem] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setExpense(null);
    setProblem(false);
    api
      .getExpense(administrationId, expenseId)
      .then((view) => {
        if (!cancelled) setExpense(view);
      })
      .catch(() => {
        if (!cancelled) setProblem(true);
      });
    return () => {
      cancelled = true;
    };
  }, [administrationId, api, expenseId]);

  if (problem) {
    return (
      <p role="alert" data-testid="approve-load-error">
        {t("mobile.common.error")}
      </p>
    );
  }
  if (expense === null) {
    return (
      <p role="status" data-testid="approve-form-loading">
        {t("mobile.common.loading")}
      </p>
    );
  }

  return (
    <ExpenseForm
      administrationId={administrationId}
      expense={expense}
      api={api}
      onChanged={(next) => {
        setExpense(next);
        if (next.status !== "draft") onReady();
      }}
    />
  );
}
