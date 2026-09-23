/**
 * The Assets screen's client (`/assets`) — `api.assets.routes`.
 *
 *   POST /v1/administrations/{id}/assets
 *   GET  /v1/administrations/{id}/assets
 *   GET  /v1/administrations/{id}/assets/{asset_id}
 *   GET  /v1/administrations/{id}/assets/{asset_id}/depreciation-runs
 *   POST /v1/administrations/{id}/assets/{asset_id}/depreciate
 *   POST /v1/administrations/{id}/assets/{asset_id}/dispose
 *
 * Every amount is a Decimal-shaped STRING end to end (NFR-031).
 */

import type { DepreciationRunView, FixedAssetView } from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, unwrapList, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export interface DraftFixedAsset {
  readonly name: string;
  readonly category: string | null;
  readonly acquisitionDate: string;
  readonly acquisitionCost: string;
  readonly residualValue: string;
  readonly usefulLifeMonths: number;
  readonly assetAccountId: string;
  readonly depreciationExpenseAccountId: string;
  readonly accumulatedDepreciationAccountId: string;
}

export interface DraftDisposal {
  readonly periodId: string;
  readonly disposalDate: string;
  readonly proceeds: string;
  readonly proceedsAccountId: string | null;
  readonly gainLossAccountId: string | null;
}

export class AssetsApi {
  constructor(private readonly options: ApiOptions) {}

  createAsset(administrationId: string, draft: DraftFixedAsset): Promise<FixedAssetView> {
    return callJson<FixedAssetView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "assets"),
      {
        name: draft.name,
        category: draft.category,
        acquisition_date: draft.acquisitionDate,
        acquisition_cost: draft.acquisitionCost,
        residual_value: draft.residualValue,
        useful_life_months: draft.usefulLifeMonths,
        asset_account_id: draft.assetAccountId,
        depreciation_expense_account_id: draft.depreciationExpenseAccountId,
        accumulated_depreciation_account_id: draft.accumulatedDepreciationAccountId,
      },
    );
  }

  async listAssets(
    administrationId: string,
    options: { includeDisposed?: boolean } = {},
  ): Promise<FixedAssetView[]> {
    const raw = await callJson<readonly FixedAssetView[] | { assets: readonly FixedAssetView[] }>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "assets") +
        queryOf({ include_disposed: options.includeDisposed === false ? "false" : undefined }),
    );
    return unwrapList<FixedAssetView>(raw, "assets");
  }

  getAsset(administrationId: string, assetId: string): Promise<FixedAssetView> {
    return callJson<FixedAssetView>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "assets", assetId),
    );
  }

  async listDepreciationRuns(
    administrationId: string,
    assetId: string,
  ): Promise<DepreciationRunView[]> {
    const raw = await callJson<
      readonly DepreciationRunView[] | { runs: readonly DepreciationRunView[] }
    >(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "assets", assetId, "depreciation-runs"),
    );
    return unwrapList<DepreciationRunView>(raw, "runs");
  }

  depreciate(
    administrationId: string,
    assetId: string,
    periodId: string,
  ): Promise<DepreciationRunView> {
    return callJson<DepreciationRunView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "assets", assetId, "depreciate"),
      { period_id: periodId },
    );
  }

  dispose(
    administrationId: string,
    assetId: string,
    disposal: DraftDisposal,
  ): Promise<FixedAssetView> {
    return callJson<FixedAssetView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "assets", assetId, "dispose"),
      {
        period_id: disposal.periodId,
        disposal_date: disposal.disposalDate,
        proceeds: disposal.proceeds,
        proceeds_account_id: disposal.proceedsAccountId,
        gain_loss_account_id: disposal.gainLossAccountId,
      },
    );
  }
}
