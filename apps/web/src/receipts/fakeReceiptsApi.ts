/** An in-memory `ReceiptsApiShape` for tests; records every call. Not imported by the app. */

import type { MissingReceiptView } from "@ledgr/shared-types";

import type { ReceiptsApiShape } from "./api";

export const fakeMissing: readonly MissingReceiptView[] = [
  {
    bank_transaction_id: "bt-1",
    booking_date: "2026-09-12",
    amount: "70.27",
    counterparty: "KPN B.V.",
    description: "KPN factuur september",
  },
  {
    bank_transaction_id: "bt-2",
    booking_date: "2026-09-20",
    amount: "1234.56",
    counterparty: null,
    description: null,
  },
];

export interface FakeReceiptsApi extends ReceiptsApiShape {
  readonly calls: { method: string; args: unknown[] }[];
}

export function fakeReceiptsApi(
  items: readonly MissingReceiptView[] = fakeMissing,
): FakeReceiptsApi {
  const calls: { method: string; args: unknown[] }[] = [];
  return {
    calls,
    listMissing: async (administrationId) => {
      calls.push({ method: "listMissing", args: [administrationId] });
      return { items, count: items.length };
    },
  };
}
