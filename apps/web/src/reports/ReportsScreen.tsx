import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { BalanceSheetView, IncomeStatementView, ReportLineView } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { sumDecimals } from "../ledger/api";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";

/**
 * `/reports` — balance sheet and income statement, both computed entirely
 * from `ledger.balances_as_of`. Two tabs, the same `?view=` pattern
 * `/ledger` and `/journal` use.
 */
type View = "balance-sheet" | "income-statement";
const VIEWS: readonly View[] = ["balance-sheet", "income-statement"];

export function ReportsScreen() {
  const { t } = useI18n();
  const [params] = useSearchParams();
  const raw = params.get("view");
  const view: View = VIEWS.find((candidate) => candidate === raw) ?? "balance-sheet";
  const { administration, fiscalYear } = useAdministration();

  return (
    <section className="screen" aria-label={t("reports.title")} data-testid="reports-screen">
      <PageHeader
        title={t("reports.title")}
        context={t("ledger.fiscal_year_context", {
          start: fiscalYear.start_date.slice(0, 4),
          end: fiscalYear.end_date.slice(0, 4),
        })}
      />
      <nav className="subnav" aria-label={t("reports.title")}>
        {VIEWS.map((candidate) => (
          <Link
            key={candidate}
            to={`/reports?view=${candidate}`}
            aria-current={candidate === view ? "page" : undefined}
            data-testid={`reports-view-${candidate}`}
          >
            {t(`reports.tab.${candidate}`)}
          </Link>
        ))}
      </nav>
      {view === "balance-sheet" ? (
        <BalanceSheet administrationId={administration.id} fiscalYearId={fiscalYear.id} />
      ) : null}
      {view === "income-statement" ? (
        <IncomeStatement administrationId={administration.id} fiscalYearId={fiscalYear.id} />
      ) : null}
    </section>
  );
}

function ReportLines({ lines }: { lines: readonly ReportLineView[] }) {
  const { money } = useI18n();
  if (lines.length === 0) return null;
  return (
    <tbody>
      {lines.map((line) => (
        <tr key={line.account_id}>
          <td className="ledgr-num">{line.account_code}</td>
          <td>{line.account_name}</td>
          <td className="table__num">{money(line.amount)}</td>
        </tr>
      ))}
    </tbody>
  );
}

function BalanceSheet({
  administrationId,
  fiscalYearId,
}: {
  administrationId: string;
  fiscalYearId: string;
}) {
  const { t, money, date } = useI18n();
  const { reports } = useServices();
  const [sheet, setSheet] = useState<BalanceSheetView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setSheet(null);
    setProblem(null);
    reports
      .getBalanceSheet(administrationId, fiscalYearId)
      .then((result) => {
        if (!cancelled) setSheet(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [reports, administrationId, fiscalYearId, attempt]);

  if (problem !== null)
    return <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />;
  if (sheet === null) return <LoadingSkeleton rows={6} />;

  return (
    <div className="panel table-wrap" data-testid="balance-sheet">
      <p className="caption">{t("reports.as_of", { date: date(sheet.as_of) })}</p>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">{t("ledger.column.account")}</th>
            <th scope="col">{t("ledger.column.name")}</th>
            <th scope="col" className="table__num">
              {t("ledger.column.amount")}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th colSpan={3} scope="colgroup" className="table__section">
              {t("reports.section.assets")}
            </th>
          </tr>
        </tbody>
        <ReportLines lines={sheet.assets} />
        <tbody>
          <tr>
            <td colSpan={2}>
              <strong>{t("reports.total_assets")}</strong>
            </td>
            <td className="table__num" data-testid="total-assets">
              <strong>{money(sheet.total_assets)}</strong>
            </td>
          </tr>
          <tr>
            <th colSpan={3} scope="colgroup" className="table__section">
              {t("reports.section.liabilities")}
            </th>
          </tr>
        </tbody>
        <ReportLines lines={sheet.liabilities} />
        <tbody>
          <tr>
            <th colSpan={3} scope="colgroup" className="table__section">
              {t("reports.section.equity")}
            </th>
          </tr>
        </tbody>
        <ReportLines lines={sheet.equity} />
        <tbody>
          <tr>
            <td>{t("reports.current_year_result")}</td>
            <td />
            <td className="table__num">{money(sheet.current_year_result)}</td>
          </tr>
          <tr>
            <td colSpan={2}>
              <strong>{t("reports.total_liabilities_and_equity")}</strong>
            </td>
            <td className="table__num" data-testid="total-liabilities-and-equity">
              <strong>{money(sumDecimals([sheet.total_liabilities, sheet.total_equity]))}</strong>
            </td>
          </tr>
        </tbody>
      </table>
      <p aria-live="polite">
        {sheet.is_balanced ? (
          <span className="chip chip--positive">{t("ledger.trial_balance.balanced")}</span>
        ) : (
          <span className="chip chip--attention">{t("ledger.trial_balance.unbalanced")}</span>
        )}
      </p>
    </div>
  );
}

function IncomeStatement({
  administrationId,
  fiscalYearId,
}: {
  administrationId: string;
  fiscalYearId: string;
}) {
  const { t, money, date } = useI18n();
  const { reports } = useServices();
  const [statement, setStatement] = useState<IncomeStatementView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setStatement(null);
    setProblem(null);
    reports
      .getIncomeStatement(administrationId, fiscalYearId)
      .then((result) => {
        if (!cancelled) setStatement(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [reports, administrationId, fiscalYearId, attempt]);

  if (problem !== null)
    return <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />;
  if (statement === null) return <LoadingSkeleton rows={6} />;

  return (
    <div className="panel table-wrap" data-testid="income-statement">
      <p className="caption">
        {t("reports.period", {
          start: date(statement.period_start),
          end: date(statement.period_end),
        })}
      </p>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">{t("ledger.column.account")}</th>
            <th scope="col">{t("ledger.column.name")}</th>
            <th scope="col" className="table__num">
              {t("ledger.column.amount")}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th colSpan={3} scope="colgroup" className="table__section">
              {t("reports.section.revenue")}
            </th>
          </tr>
        </tbody>
        <ReportLines lines={statement.revenue} />
        <tbody>
          <tr>
            <td colSpan={2}>
              <strong>{t("reports.total_revenue")}</strong>
            </td>
            <td className="table__num" data-testid="total-revenue">
              <strong>{money(statement.total_revenue)}</strong>
            </td>
          </tr>
          <tr>
            <th colSpan={3} scope="colgroup" className="table__section">
              {t("reports.section.expense")}
            </th>
          </tr>
        </tbody>
        <ReportLines lines={statement.expense} />
        <tbody>
          <tr>
            <td colSpan={2}>
              <strong>{t("reports.total_expense")}</strong>
            </td>
            <td className="table__num" data-testid="total-expense">
              <strong>{money(statement.total_expense)}</strong>
            </td>
          </tr>
          <tr>
            <td colSpan={2}>
              <strong>{t("reports.net_result")}</strong>
            </td>
            <td className="table__num" data-testid="net-result">
              <strong>{money(statement.net_result)}</strong>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
