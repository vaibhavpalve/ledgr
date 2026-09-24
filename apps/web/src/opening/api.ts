/**
 * The opening balance's client (`/ledger/opening-balance`) — `api.opening.routes` (ADR-088).
 *
 *   GET  /v1/administrations/{id}/opening-balance?fiscal_year_id=
 *   POST /v1/administrations/{id}/opening-balance
 *
 * Every amount is a Decimal-shaped STRING (NFR-031).
 */

import type { OpeningBalanceView, OpeningEntryView } from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, type ApiOptions } from "../api/http";

export interface OpeningBalanceBody {
  readonly fiscal_year_id: string;
  readonly lines: readonly { account_id: string; debit: string; credit: string }[];
  readonly balance_account_id: string | null;
}

export class OpeningApi {
  constructor(private readonly options: ApiOptions) {}

  getOpeningBalance(administrationId: string, fiscalYearId: string): Promise<OpeningBalanceView> {
    return callJson<OpeningBalanceView>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "opening-balance") +
        queryOf({ fiscal_year_id: fiscalYearId }),
    );
  }

  async postOpeningBalance(
    administrationId: string,
    body: OpeningBalanceBody,
  ): Promise<OpeningEntryView> {
    const result = await callJson<{ posted: OpeningEntryView }>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "opening-balance"),
      body,
    );
    return result.posted;
  }
}
