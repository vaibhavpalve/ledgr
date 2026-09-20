/**
 * The Grootboek screen's reads — docs/founder-review-2026-09-14.md §4.3.
 *
 *   GET /v1/administrations/{id}/chart-of-accounts
 *   GET /v1/administrations/{id}/trial-balance?fiscal_year_id=
 *   GET /v1/administrations/{id}/journal-entries?fiscal_year_id=&cursor=&limit=
 *   GET /v1/administrations/{id}/journal-entries/{entry_id}
 *
 * Reads only. There is deliberately no write here: postings reach the ledger
 * through expenses and invoices (ADR-033, ADR-039), never from a screen that
 * lists them, and FR-GL-003 means the one correction a person can make to a
 * posted entry is a reversing entry — which is its own future screen, not a
 * method on this client.
 *
 * Every amount is a STRING (NFR-031), handed to `money()` unchanged. The
 * `to*` functions are the seams where a shape difference against the real
 * backend gets reconciled, in this client and nowhere else.
 */

import type {
  ChartAccountView,
  JournalEntryPageView,
  JournalEntrySummaryView,
  JournalEntryView,
  TrialBalanceRowView,
  TrialBalanceView,
} from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, unwrapList, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export class LedgerApi {
  constructor(private readonly options: ApiOptions) {}

  async listChartOfAccounts(administrationId: string): Promise<ChartAccountView[]> {
    const raw = await callJson<
      readonly ChartAccountView[] | { accounts: readonly ChartAccountView[] }
    >(this.options, "GET", pathOf("v1", "administrations", administrationId, "chart-of-accounts"));
    return unwrapList<ChartAccountView>(raw, "accounts");
  }

  async getTrialBalance(administrationId: string, fiscalYearId: string): Promise<TrialBalanceView> {
    const raw = await callJson<RawTrialBalance>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "trial-balance") +
        queryOf({ fiscal_year_id: fiscalYearId }),
    );
    return toTrialBalance(raw, fiscalYearId);
  }

  async listJournalEntries(
    administrationId: string,
    params: { fiscalYearId: string; cursor?: string | null; limit?: number },
  ): Promise<JournalEntryPageView> {
    const raw = await callJson<RawEntryPage>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "journal-entries") +
        queryOf({
          fiscal_year_id: params.fiscalYearId,
          cursor: params.cursor,
          limit: params.limit,
        }),
    );
    return toEntryPage(raw);
  }

  getJournalEntry(administrationId: string, entryId: string): Promise<JournalEntryView> {
    return callJson<JournalEntryView>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "journal-entries", entryId),
    );
  }
}

interface WrappedTrialBalance {
  fiscal_year_id?: string;
  rows: readonly TrialBalanceRowView[];
  total_debit?: string;
  total_credit?: string;
}

type RawTrialBalance = readonly TrialBalanceRowView[] | WrappedTrialBalance;

function toTrialBalance(raw: RawTrialBalance, fiscalYearId: string): TrialBalanceView {
  const rows = unwrapList<TrialBalanceRowView>(raw, "rows");
  const totals: Partial<WrappedTrialBalance> = Array.isArray(raw)
    ? {}
    : (raw as WrappedTrialBalance);
  return {
    fiscal_year_id: totals.fiscal_year_id ?? fiscalYearId,
    rows,
    // Summed here only when the server did not: string decimal addition,
    // never `Number()` (NFR-031).
    total_debit: totals.total_debit ?? sumDecimals(rows.map((row) => row.total_debit)),
    total_credit: totals.total_credit ?? sumDecimals(rows.map((row) => row.total_credit)),
  };
}

interface WrappedEntryPage {
  entries?: readonly JournalEntrySummaryView[];
  items?: readonly JournalEntrySummaryView[];
  next_cursor?: string | null;
}

type RawEntryPage = readonly JournalEntrySummaryView[] | WrappedEntryPage;

function toEntryPage(raw: RawEntryPage): JournalEntryPageView {
  if (Array.isArray(raw))
    return { entries: [...(raw as readonly JournalEntrySummaryView[])], next_cursor: null };
  const page = raw as WrappedEntryPage;
  return {
    entries: [...(page.entries ?? page.items ?? [])],
    next_cursor: page.next_cursor ?? null,
  };
}

/**
 * Adds decimal strings of scale ≤ 2 exactly, digit by digit — the only
 * arithmetic in this module, and it exists so that a trial balance's footer
 * can be shown when the server sent rows without totals. Inputs that are
 * not plain decimals are refused rather than coerced, the same posture
 * `@ledgr/i18n`'s `formatMoney` takes.
 */
export function sumDecimals(values: readonly string[]): string {
  let cents = 0n;
  for (const value of values) {
    const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(value);
    if (match === null) throw new Error(`not an exact decimal: ${JSON.stringify(value)}`);
    const [, sign = "", whole = "0", fraction = ""] = match;
    const magnitude = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
    cents += sign === "-" ? -magnitude : magnitude;
  }
  const negative = cents < 0n;
  const abs = negative ? -cents : cents;
  const whole = (abs / 100n).toString();
  const fraction = (abs % 100n).toString().padStart(2, "0");
  return `${negative ? "-" : ""}${whole}.${fraction}`;
}
