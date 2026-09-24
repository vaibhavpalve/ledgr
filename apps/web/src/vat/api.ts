/**
 * The BTW screen's client (`/vat`) — `api.vat_returns.routes` (ADR-087).
 *
 *   GET  /v1/administrations/{id}/vat-returns?fiscal_year_id=
 *   GET  /v1/administrations/{id}/vat-returns/{period_id}
 *   GET  /v1/administrations/{id}/vat-returns/{period_id}/boxes/{code}/lines
 *   POST /v1/administrations/{id}/vat-returns/{period_id}/file
 *
 * Every amount is a Decimal-shaped STRING (NFR-031).
 */

import type { VatBoxLineView, VatReturnView } from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export interface FileVatReturnBody {
  readonly filing_reference?: string | null;
  readonly acknowledged_warnings: readonly string[];
  /** The total the filer reviewed; the server refuses if the ledger now says otherwise. */
  readonly expected_total: string;
}

export class VatApi {
  constructor(private readonly options: ApiOptions) {}

  private base(administrationId: string): string {
    return pathOf("v1", "administrations", administrationId, "vat-returns");
  }

  async listReturns(
    administrationId: string,
    fiscalYearId: string,
  ): Promise<readonly VatReturnView[]> {
    const body = await callJson<{ returns: readonly VatReturnView[] }>(
      this.options,
      "GET",
      this.base(administrationId) + queryOf({ fiscal_year_id: fiscalYearId }),
    );
    return body.returns;
  }

  getReturn(administrationId: string, periodId: string): Promise<VatReturnView> {
    return callJson<VatReturnView>(
      this.options,
      "GET",
      `${this.base(administrationId)}/${encodeURIComponent(periodId)}`,
    );
  }

  async getBoxLines(
    administrationId: string,
    periodId: string,
    code: string,
  ): Promise<readonly VatBoxLineView[]> {
    const body = await callJson<{ lines: readonly VatBoxLineView[] }>(
      this.options,
      "GET",
      `${this.base(administrationId)}/${encodeURIComponent(periodId)}/boxes/${encodeURIComponent(code)}/lines`,
    );
    return body.lines;
  }

  fileReturn(
    administrationId: string,
    periodId: string,
    body: FileVatReturnBody,
  ): Promise<VatReturnView> {
    return callJson<VatReturnView>(
      this.options,
      "POST",
      `${this.base(administrationId)}/${encodeURIComponent(periodId)}/file`,
      body,
    );
  }
}
