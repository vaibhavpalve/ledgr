/**
 * The client's own list of bank payments that still need a receipt (docs/firm-home/
 * contract-wave2.md, backend-chasing):
 *
 *   GET /v1/administrations/{id}/missing-receipts
 *       -> { items: [ { bank_transaction_id, booking_date, amount, counterparty, description } ],
 *            count }
 *
 * Read only. Amounts stay decimal strings (NFR-031); the upload itself goes through the existing
 * capture flow, never a second uploader.
 */

import type { MissingReceiptsView } from "@ledgr/shared-types";

import { callJson, pathOf, type ApiOptions } from "../api/http";

export interface ReceiptsApiShape {
  listMissing(administrationId: string): Promise<MissingReceiptsView>;
}

export class ReceiptsApi implements ReceiptsApiShape {
  constructor(private readonly options: ApiOptions) {}

  async listMissing(administrationId: string): Promise<MissingReceiptsView> {
    const raw = await callJson<Partial<MissingReceiptsView>>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "missing-receipts"),
    );
    const items = Array.isArray(raw.items) ? raw.items : [];
    return { items, count: typeof raw.count === "number" ? raw.count : items.length };
  }
}
