/**
 * The firm home's client ("To do") - docs/firm-home/contract.md.
 *
 *   GET  /v1/firm/summary                     the six counts and "since you were away"
 *   POST /v1/firm/summary/seen                moves the "since" window forward
 *   GET  /v1/firm/worklist                    one row per client, chip/q/assigned/sort/page
 *   POST /v1/firm/clients/{id}/snooze
 *   POST /v1/firm/clients/assign              partial failure is listed, never a 500
 *   GET  /v1/firm/staff
 *   GET  /v1/firm/deadlines
 *   GET  /v1/firm/proposals                   booking proposals, grouped (ADR-110)
 *   POST /v1/firm/proposals/decide
 *   GET  /v1/firm/inbox                       client replies (question threads)
 *
 * Built on `api/http`'s `callJson`, so every mutating call carries a fresh Idempotency-Key
 * (NFR-032) and every call carries Accept-Language (FR-UX-007). Responses stay in the wire's
 * snake_case, as `@ledgr/shared-types` declares them - the convention every client but
 * `ClientApi` follows. Amounts are decimal strings end to end (NFR-031).
 *
 * Nothing here is a second authorization check: the server restricts every answer to the
 * administrations the caller holds a live grant on (ADR-109).
 */

import type {
  FirmAssignResultView,
  FirmDeadlineView,
  FirmDecideResultView,
  FirmInboxView,
  FirmProposalDecision,
  FirmProposalsView,
  FirmStaffView,
  FirmSummaryView,
  FirmWorklistChip,
  FirmWorklistSort,
  FirmWorklistView,
} from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, unwrapList, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export interface WorklistQuery {
  readonly chip: FirmWorklistChip;
  readonly q: string;
  /** `me`, `any`, or a staff member's user id. */
  readonly assigned: string;
  readonly sort: FirmWorklistSort;
  readonly dir: "asc" | "desc";
  readonly page: number;
  readonly pageSize: number;
}

export interface FirmApiShape {
  getSummary(since?: string): Promise<FirmSummaryView>;
  markSeen(): Promise<void>;
  getWorklist(query: WorklistQuery): Promise<FirmWorklistView>;
  snooze(administrationId: string, until: string | null, reason: string): Promise<void>;
  assign(
    administrationIds: readonly string[],
    userId: string | null,
  ): Promise<FirmAssignResultView>;
  listStaff(): Promise<FirmStaffView[]>;
  getDeadlines(): Promise<FirmDeadlineView[]>;
  listProposals(administrationIds?: readonly string[]): Promise<FirmProposalsView>;
  decideProposals(
    decisions: readonly { proposalId: string; decision: FirmProposalDecision }[],
  ): Promise<FirmDecideResultView>;
  getInbox(options?: { unread?: boolean; limit?: number }): Promise<FirmInboxView>;
}

export class FirmApi implements FirmApiShape {
  constructor(private readonly options: ApiOptions) {}

  getSummary(since?: string): Promise<FirmSummaryView> {
    return callJson<FirmSummaryView>(
      this.options,
      "GET",
      `${pathOf("v1", "firm", "summary")}${queryOf({ since })}`,
    );
  }

  async markSeen(): Promise<void> {
    await callJson<void>(this.options, "POST", pathOf("v1", "firm", "summary", "seen"), {});
  }

  getWorklist(query: WorklistQuery): Promise<FirmWorklistView> {
    return callJson<FirmWorklistView>(
      this.options,
      "GET",
      `${pathOf("v1", "firm", "worklist")}${queryOf({
        chip: query.chip,
        q: query.q.trim(),
        assigned: query.assigned,
        sort: query.sort,
        dir: query.dir,
        page: query.page,
        page_size: query.pageSize,
      })}`,
    );
  }

  async snooze(administrationId: string, until: string | null, reason: string): Promise<void> {
    await callJson<void>(
      this.options,
      "POST",
      pathOf("v1", "firm", "clients", administrationId, "snooze"),
      { until, reason },
    );
  }

  assign(
    administrationIds: readonly string[],
    userId: string | null,
  ): Promise<FirmAssignResultView> {
    return callJson<FirmAssignResultView>(
      this.options,
      "POST",
      pathOf("v1", "firm", "clients", "assign"),
      { administration_ids: administrationIds, user_id: userId },
    );
  }

  async listStaff(): Promise<FirmStaffView[]> {
    const raw = await callJson<readonly FirmStaffView[] | { staff: readonly FirmStaffView[] }>(
      this.options,
      "GET",
      pathOf("v1", "firm", "staff"),
    );
    return unwrapList<FirmStaffView>(raw, "staff");
  }

  async getDeadlines(): Promise<FirmDeadlineView[]> {
    const raw = await callJson<
      readonly FirmDeadlineView[] | { deadlines: readonly FirmDeadlineView[] }
    >(this.options, "GET", pathOf("v1", "firm", "deadlines"));
    return unwrapList<FirmDeadlineView>(raw, "deadlines");
  }

  listProposals(administrationIds?: readonly string[]): Promise<FirmProposalsView> {
    const ids =
      administrationIds !== undefined && administrationIds.length > 0
        ? administrationIds.join(",")
        : undefined;
    return callJson<FirmProposalsView>(
      this.options,
      "GET",
      `${pathOf("v1", "firm", "proposals")}${queryOf({ administration_ids: ids })}`,
    );
  }

  decideProposals(
    decisions: readonly { proposalId: string; decision: FirmProposalDecision }[],
  ): Promise<FirmDecideResultView> {
    return callJson<FirmDecideResultView>(
      this.options,
      "POST",
      pathOf("v1", "firm", "proposals", "decide"),
      {
        decisions: decisions.map((entry) => ({
          proposal_id: entry.proposalId,
          decision: entry.decision,
        })),
      },
    );
  }

  getInbox(options: { unread?: boolean; limit?: number } = {}): Promise<FirmInboxView> {
    return callJson<FirmInboxView>(
      this.options,
      "GET",
      `${pathOf("v1", "firm", "inbox")}${queryOf({
        unread: options.unread === true ? "true" : undefined,
        limit: options.limit,
      })}`,
    );
  }
}
