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
 *   GET  /v1/firm/inbox                       question threads; awaiting=firm is client replies
 *
 * Wave 2 (docs/firm-home/contract-wave2.md):
 *   POST /v1/firm/chase/preview|send          "Request missing receipts" (ADR-114)
 *   GET|POST /v1/firm/views[/{id}/rename|archive]   saved views, per user (ADR-115)
 *   GET  /v1/firm/worklist/next               the next client in the same query
 *   /v1/administrations/{id}/rules, rule-postings, chase-setting   one client (ADR-113/114)
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
  BookingRuleView,
  ChaseSettingView,
  FirmAssignResultView,
  FirmChasePreviewView,
  FirmChaseSendResultView,
  FirmDeadlineView,
  FirmDecideResultView,
  FirmInboxAwaiting,
  FirmInboxView,
  FirmNextClientView,
  FirmProposalDecision,
  FirmProposalsView,
  FirmSavedView,
  FirmSavedViewQuery,
  FirmStaffView,
  FirmSummaryView,
  FirmVatFrequency,
  FirmWorklistChip,
  FirmWorklistSort,
  FirmWorklistView,
  RulePostingView,
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
  /** "More filters" (wave 2): only clients filing BTW at this frequency. */
  readonly vatFrequency?: FirmVatFrequency | null;
}

export interface ProposalDecisionInput {
  readonly proposalId: string;
  readonly decision: FirmProposalDecision;
  /** "Always do this for these clients" (ADR-113): approve only. */
  readonly remember?: boolean;
}

/** The worklist query without paging - what a saved view stores and `/next` follows. */
export function savedQueryOf(query: WorklistQuery): FirmSavedViewQuery {
  return {
    chip: query.chip,
    q: query.q.trim(),
    assigned: query.assigned,
    sort: query.sort,
    dir: query.dir,
    vat_frequency: query.vatFrequency ?? null,
  };
}

function queryParams(query: FirmSavedViewQuery) {
  return {
    chip: query.chip,
    q: query.q.trim(),
    assigned: query.assigned,
    sort: query.sort,
    dir: query.dir,
    vat_frequency: query.vat_frequency ?? undefined,
  };
}

export interface InboxOptions {
  readonly unread?: boolean;
  readonly limit?: number;
  /** `firm` = replies to you (the panel and the badge); `client` = questions still out. */
  readonly awaiting?: FirmInboxAwaiting;
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
  decideProposals(decisions: readonly ProposalDecisionInput[]): Promise<FirmDecideResultView>;
  getInbox(options?: InboxOptions): Promise<FirmInboxView>;

  // --- wave 2 (docs/firm-home/contract-wave2.md) ---
  previewChase(administrationIds: readonly string[]): Promise<FirmChasePreviewView>;
  sendChase(administrationIds: readonly string[]): Promise<FirmChaseSendResultView>;
  listViews(): Promise<FirmSavedView[]>;
  createView(name: string, query: FirmSavedViewQuery): Promise<FirmSavedView>;
  renameView(viewId: string, name: string): Promise<void>;
  archiveView(viewId: string): Promise<void>;
  nextClient(after: string, query: FirmSavedViewQuery): Promise<FirmNextClientView>;
  listRules(administrationId: string): Promise<BookingRuleView[]>;
  retireRule(administrationId: string, ruleId: string): Promise<void>;
  setRuleMaxAmount(
    administrationId: string,
    ruleId: string,
    maxAmount: string | null,
  ): Promise<void>;
  listRulePostings(administrationId: string, limit?: number): Promise<RulePostingView[]>;
  undoRulePosting(administrationId: string, proposalId: string): Promise<void>;
  getChaseSetting(administrationId: string): Promise<ChaseSettingView>;
  setChaseSetting(administrationId: string, setting: ChaseSettingView): Promise<ChaseSettingView>;
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
        vat_frequency: query.vatFrequency ?? undefined,
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

  decideProposals(decisions: readonly ProposalDecisionInput[]): Promise<FirmDecideResultView> {
    return callJson<FirmDecideResultView>(
      this.options,
      "POST",
      pathOf("v1", "firm", "proposals", "decide"),
      {
        decisions: decisions.map((entry) => ({
          proposal_id: entry.proposalId,
          decision: entry.decision,
          // Sent only when asked for, and only on an approval (contract-wave2: approve only).
          ...(entry.remember === true && entry.decision === "approve" ? { remember: true } : {}),
        })),
      },
    );
  }

  getInbox(options: InboxOptions = {}): Promise<FirmInboxView> {
    return callJson<FirmInboxView>(
      this.options,
      "GET",
      `${pathOf("v1", "firm", "inbox")}${queryOf({
        unread: options.unread === true ? "true" : undefined,
        limit: options.limit,
        awaiting: options.awaiting,
      })}`,
    );
  }

  // --- wave 2 ---

  previewChase(administrationIds: readonly string[]): Promise<FirmChasePreviewView> {
    return callJson<FirmChasePreviewView>(
      this.options,
      "POST",
      pathOf("v1", "firm", "chase", "preview"),
      { administration_ids: administrationIds },
    );
  }

  sendChase(administrationIds: readonly string[]): Promise<FirmChaseSendResultView> {
    return callJson<FirmChaseSendResultView>(
      this.options,
      "POST",
      pathOf("v1", "firm", "chase", "send"),
      { administration_ids: administrationIds },
    );
  }

  async listViews(): Promise<FirmSavedView[]> {
    const raw = await callJson<readonly FirmSavedView[] | { views: readonly FirmSavedView[] }>(
      this.options,
      "GET",
      pathOf("v1", "firm", "views"),
    );
    return unwrapList<FirmSavedView>(raw, "views");
  }

  createView(name: string, query: FirmSavedViewQuery): Promise<FirmSavedView> {
    return callJson<FirmSavedView>(this.options, "POST", pathOf("v1", "firm", "views"), {
      name,
      query,
    });
  }

  async renameView(viewId: string, name: string): Promise<void> {
    await callJson<unknown>(this.options, "POST", pathOf("v1", "firm", "views", viewId, "rename"), {
      name,
    });
  }

  async archiveView(viewId: string): Promise<void> {
    await callJson<unknown>(
      this.options,
      "POST",
      pathOf("v1", "firm", "views", viewId, "archive"),
      {},
    );
  }

  nextClient(after: string, query: FirmSavedViewQuery): Promise<FirmNextClientView> {
    return callJson<FirmNextClientView>(
      this.options,
      "GET",
      `${pathOf("v1", "firm", "worklist", "next")}${queryOf({ after, ...queryParams(query) })}`,
    );
  }

  async listRules(administrationId: string): Promise<BookingRuleView[]> {
    const raw = await callJson<readonly BookingRuleView[] | { rules: readonly BookingRuleView[] }>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "rules"),
    );
    return unwrapList<BookingRuleView>(raw, "rules");
  }

  async retireRule(administrationId: string, ruleId: string): Promise<void> {
    await callJson<unknown>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "rules", ruleId, "retire"),
      {},
    );
  }

  async setRuleMaxAmount(
    administrationId: string,
    ruleId: string,
    maxAmount: string | null,
  ): Promise<void> {
    await callJson<unknown>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "rules", ruleId, "max-amount"),
      { max_amount: maxAmount },
    );
  }

  async listRulePostings(administrationId: string, limit?: number): Promise<RulePostingView[]> {
    const raw = await callJson<readonly RulePostingView[] | { items: readonly RulePostingView[] }>(
      this.options,
      "GET",
      `${pathOf("v1", "administrations", administrationId, "rule-postings")}${queryOf({ limit })}`,
    );
    return unwrapList<RulePostingView>(raw, "items");
  }

  async undoRulePosting(administrationId: string, proposalId: string): Promise<void> {
    await callJson<unknown>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "rule-postings", proposalId, "undo"),
      {},
    );
  }

  getChaseSetting(administrationId: string): Promise<ChaseSettingView> {
    return callJson<ChaseSettingView>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "chase-setting"),
    );
  }

  setChaseSetting(administrationId: string, setting: ChaseSettingView): Promise<ChaseSettingView> {
    return callJson<ChaseSettingView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "chase-setting"),
      { enabled: setting.enabled, cadence: setting.cadence },
    );
  }
}
