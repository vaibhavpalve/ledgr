/**
 * The Reports screen's client (`/reports`) — `api.reports.routes`.
 *
 *   GET /v1/administrations/{id}/reports/balance-sheet?fiscal_year_id=&as_of=
 *   GET /v1/administrations/{id}/reports/income-statement?fiscal_year_id=&period_start=&period_end=
 *
 * Every amount is a Decimal-shaped STRING (NFR-031).
 */

import type { BalanceSheetView, IncomeStatementView } from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export class ReportsApi {
  constructor(private readonly options: ApiOptions) {}

  getBalanceSheet(
    administrationId: string,
    fiscalYearId: string,
    asOf?: string,
  ): Promise<BalanceSheetView> {
    return callJson<BalanceSheetView>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "reports", "balance-sheet") +
        queryOf({ fiscal_year_id: fiscalYearId, as_of: asOf }),
    );
  }

  getIncomeStatement(
    administrationId: string,
    fiscalYearId: string,
    periodStart?: string,
    periodEnd?: string,
  ): Promise<IncomeStatementView> {
    return callJson<IncomeStatementView>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "reports", "income-statement") +
        queryOf({
          fiscal_year_id: fiscalYearId,
          period_start: periodStart,
          period_end: periodEnd,
        }),
    );
  }
}
