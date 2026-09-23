/**
 * The Journal screen's writes (`/journal`) — `api.ledger.routes`.
 *
 *   GET  /v1/administrations/{id}/journals
 *   POST /v1/administrations/{id}/journal-entries
 *   POST /v1/administrations/{id}/journal-entries/{entry_id}/reverse
 *   GET  /v1/administrations/{id}/periods?fiscal_year_id=
 *   POST /v1/administrations/{id}/periods/{period_id}/lock
 *   POST /v1/administrations/{id}/periods/{period_id}/unlock
 *
 * `ledger/api.ts` is the reads (posted entries, trial balance, chart); this
 * is the writes the engine already had and no screen had ever reached. Every
 * amount is a Decimal-shaped STRING end to end (NFR-031) — this client never
 * calls `Number()` on one, and neither should a caller.
 */

import type {
  JournalDefView,
  PeriodView,
  PostedJournalEntryView,
} from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, unwrapList, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export interface DraftJournalLine {
  readonly accountId: string;
  readonly debit: string;
  readonly credit: string;
  readonly description: string | null;
}

export class JournalApi {
  constructor(private readonly options: ApiOptions) {}

  async listJournals(administrationId: string): Promise<JournalDefView[]> {
    const raw = await callJson<readonly JournalDefView[] | { journals: readonly JournalDefView[] }>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "journals"),
    );
    return unwrapList<JournalDefView>(raw, "journals");
  }

  async listPeriods(administrationId: string, fiscalYearId: string): Promise<PeriodView[]> {
    const raw = await callJson<readonly PeriodView[] | { periods: readonly PeriodView[] }>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "periods") +
        queryOf({ fiscal_year_id: fiscalYearId }),
    );
    return unwrapList<PeriodView>(raw, "periods");
  }

  postEntry(
    administrationId: string,
    entry: {
      journalId: string;
      periodId: string;
      entryDate: string;
      description: string;
      documentReference?: string | null;
      lines: readonly DraftJournalLine[];
    },
  ): Promise<PostedJournalEntryView> {
    return callJson<PostedJournalEntryView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "journal-entries"),
      {
        journal_id: entry.journalId,
        period_id: entry.periodId,
        entry_date: entry.entryDate,
        description: entry.description,
        document_reference: entry.documentReference ?? null,
        lines: entry.lines.map((line) => ({
          account_id: line.accountId,
          debit: line.debit,
          credit: line.credit,
          description: line.description,
        })),
      },
    );
  }

  reverseEntry(
    administrationId: string,
    entryId: string,
    reversal: { periodId: string; entryDate: string; description?: string | null },
  ): Promise<PostedJournalEntryView> {
    return callJson<PostedJournalEntryView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "journal-entries", entryId, "reverse"),
      {
        period_id: reversal.periodId,
        entry_date: reversal.entryDate,
        description: reversal.description ?? null,
      },
    );
  }

  lockPeriod(administrationId: string, periodId: string): Promise<PeriodView> {
    return callJson<PeriodView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "periods", periodId, "lock"),
    );
  }

  unlockPeriod(administrationId: string, periodId: string, reason: string): Promise<PeriodView> {
    return callJson<PeriodView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "periods", periodId, "unlock"),
      { reason },
    );
  }
}
