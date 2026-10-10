/**
 * An in-memory `FirmApiShape` for tests (and for opening the screen without a backend): the
 * contract's own example shapes (docs/firm-home/contract.md), with every call recorded so a test
 * can assert what was sent. Any method can be replaced through `overrides`, which is how a test
 * makes one panel fail while the rest of the page answers.
 *
 * Not imported by the app itself, so it never reaches the production bundle.
 */

import type {
  BookingRuleView,
  ChaseSettingView,
  FirmChasePreviewView,
  FirmClientRef,
  FirmNextClientView,
  FirmSavedView,
  RulePostingView,
  FirmDeadlineView,
  FirmInboxAwaiting,
  FirmInboxView,
  FirmProposalsView,
  FirmStaffView,
  FirmSummaryView,
  FirmWorklistRowView,
  FirmWorklistView,
} from "@ledgr/shared-types";

import type { FirmApiShape, WorklistQuery } from "./api";

export const fakeSummary: FirmSummaryView = {
  previous_login_at: "2026-10-01T08:30:00+00:00",
  since: "2026-10-01T08:30:00+00:00",
  client_count: 3,
  counts: {
    auto_bookings: 5,
    receipts_to_book: 14,
    missing_receipts: 6,
    bank_to_match: 7,
    open_questions: 3,
    broken_feeds: 1,
  },
  activity: [
    {
      kind: "receipts_uploaded",
      count: 12,
      client_count: 2,
      clients: [
        { administration_id: "adm-1", display_name: "Bakkerij Jansen" },
        { administration_id: "adm-2", display_name: "Eva Mulder Design" },
      ],
    },
    {
      kind: "bank_feeds_broken",
      count: 1,
      client_count: 1,
      clients: [{ administration_id: "adm-2", display_name: "Eva Mulder Design" }],
    },
  ],
};

export function fakeRow(overrides: Partial<FirmWorklistRowView> = {}): FirmWorklistRowView {
  return {
    administration_id: "adm-1",
    display_name: "Bakkerij Jansen",
    legal_name: "Bakkerij Jansen B.V.",
    kvk_number: "12345678",
    initials: "BJ",
    colour: "indigo",
    assigned_user_id: "user-1",
    assigned_name: "Sanne de Vries",
    booked_until: "2026-06-30",
    booked_until_capped_by_feed: false,
    months_behind: 3,
    counts: {
      auto_bookings: 4,
      to_book: 14,
      missing_receipts: 0,
      bank_to_match: 7,
      open_questions: 1,
    },
    waiting_on_client_since: null,
    broken_feed: null,
    vat: {
      period_label: "2026-Q3",
      frequency: "quarterly",
      status: "ready_to_review",
      due_date: "2026-10-31",
      days_to_due: 23,
    },
    snoozed_until: null,
    snooze_reason: null,
    last_chased_at: null,
    risk: 12,
    ...overrides,
  };
}

export const fakeRows: readonly FirmWorklistRowView[] = [
  fakeRow(),
  fakeRow({
    administration_id: "adm-2",
    display_name: "Eva Mulder Design",
    legal_name: "Eva Mulder Design",
    initials: "EM",
    colour: "teal",
    assigned_user_id: null,
    assigned_name: null,
    booked_until: "2026-08-31",
    booked_until_capped_by_feed: true,
    months_behind: 1,
    counts: {
      auto_bookings: 1,
      to_book: 0,
      missing_receipts: 6,
      bank_to_match: 0,
      open_questions: 2,
    },
    broken_feed: { bank_name: "ING", status: "expired" },
    vat: {
      period_label: "2026-09",
      frequency: "monthly",
      status: "not_started",
      due_date: "2026-10-13",
      days_to_due: 5,
    },
    risk: 2,
  }),
  fakeRow({
    administration_id: "adm-3",
    display_name: "Hoveniersbedrijf De Linde",
    legal_name: "Hoveniersbedrijf De Linde V.O.F.",
    initials: "HL",
    colour: "amber",
    booked_until: "2026-09-30",
    months_behind: 0,
    counts: {
      auto_bookings: 0,
      to_book: 0,
      missing_receipts: 0,
      bank_to_match: 0,
      open_questions: 0,
    },
    vat: null,
    risk: 40,
  }),
];

export function fakeWorklist(query: WorklistQuery, rows = fakeRows): FirmWorklistView {
  const needle = query.q.trim().toLowerCase();
  const matching = rows.filter((row) => row.display_name.toLowerCase().includes(needle));
  return {
    rows: matching,
    total: matching.length,
    page: query.page,
    page_size: query.pageSize,
    chip_counts: {
      my_move: 2,
      waiting_on_client: 1,
      vat_not_filed: 2,
      books_behind: 1,
      up_to_date: 1,
      snoozed: 0,
      all: 3,
    },
  };
}

export const fakeStaff: readonly FirmStaffView[] = [
  { user_id: "user-1", name: "Sanne de Vries", email: "sanne@kantoor.nl" },
  { user_id: "user-2", name: "Tom Bakker", email: "tom@kantoor.nl" },
];

export const fakeDeadlines: readonly FirmDeadlineView[] = [
  {
    kind: "vat",
    period_label: "2026-Q3",
    due_date: "2026-10-31",
    days_to_due: 23,
    client_count: 10,
    buckets: { filed: 4, ready_to_file: 2, ready_to_review: 1, in_progress: 0, not_started: 3 },
  },
];

export const fakeProposals: FirmProposalsView = {
  total: 3,
  groups: [
    {
      group_key: "kpn|4500",
      counterparty: "KPN B.V.",
      account_code: "4500",
      account_name: "Telefoon",
      count: 2,
      client_count: 2,
      total_amount: "1234.56",
      proposals: [
        {
          id: "p-1",
          administration_id: "adm-1",
          display_name: "Bakkerij Jansen",
          date: "2026-09-30",
          amount: "70.27",
          description: "KPN factuur september",
        },
        {
          id: "p-2",
          administration_id: "adm-2",
          display_name: "Eva Mulder Design",
          date: "2026-09-29",
          amount: "1164.29",
          description: "KPN zakelijk",
        },
      ],
    },
    {
      group_key: "shell|4310",
      counterparty: "Shell",
      account_code: "4310",
      account_name: "Brandstof",
      count: 1,
      client_count: 1,
      total_amount: "88.10",
      proposals: [
        {
          id: "p-3",
          administration_id: "adm-1",
          display_name: "Bakkerij Jansen",
          date: "2026-09-28",
          amount: "88.10",
          description: "Shell tankstation",
        },
      ],
    },
  ],
};

export const fakeInbox: FirmInboxView = {
  unread_count: 2,
  items: [
    {
      thread_id: "t-1",
      administration_id: "adm-2",
      display_name: "Eva Mulder Design",
      subject: "Bonnetje Coolblue",
      excerpt: "Die laptop is zakelijk, de bon staat nu in de app.",
      last_message_at: "2026-10-07T14:12:00+00:00",
      unread: true,
      awaiting: "firm",
    },
  ],
};

/** The firm's own question, still waiting on the client (`awaiting: "client"`). */
export const fakeWaitingOnClient: FirmInboxView = {
  unread_count: 0,
  items: [
    {
      thread_id: "t-2",
      administration_id: "adm-1",
      display_name: "Bakkerij Jansen",
      subject: "Betaling KPN",
      excerpt: "Waar is deze betaling van 12 september voor?",
      last_message_at: "2026-10-06T09:30:00+00:00",
      unread: false,
      awaiting: "client",
    },
  ],
};

/** Like the server: `awaiting` narrows the items and the unread count; absent or `any` is both. */
function fakeInboxFor(awaiting: FirmInboxAwaiting | undefined): FirmInboxView {
  if (awaiting === "firm") return fakeInbox;
  if (awaiting === "client") return fakeWaitingOnClient;
  return {
    unread_count: fakeInbox.unread_count + fakeWaitingOnClient.unread_count,
    items: [...fakeInbox.items, ...fakeWaitingOnClient.items],
  };
}

// --- wave 2 (docs/firm-home/contract-wave2.md) ---

export const fakeChasePreview: FirmChasePreviewView = {
  items: [
    {
      administration_id: "adm-2",
      display_name: "Eva Mulder Design",
      missing_count: 6,
      recipient_count: 1,
      last_chased_at: null,
      blocked_reason: null,
    },
    {
      administration_id: "adm-1",
      display_name: "Bakkerij Jansen",
      missing_count: 3,
      recipient_count: 2,
      last_chased_at: "2026-10-08T09:00:00+00:00",
      blocked_reason: "chased_recently",
    },
    {
      administration_id: "adm-3",
      display_name: "Hoveniersbedrijf De Linde",
      missing_count: 0,
      recipient_count: 1,
      last_chased_at: null,
      blocked_reason: "nothing_missing",
    },
  ],
};

export function fakeRule(overrides: Partial<BookingRuleView> = {}): BookingRuleView {
  return {
    id: "rule-1",
    counterparty_key: "kpn",
    counterparty_label: "KPN B.V.",
    account_code: "4500",
    account_name: "Telefoon",
    max_amount: null,
    status: "active",
    suspended_reason: null,
    created_by_name: "Sanne de Vries",
    created_at: "2026-10-02T10:00:00+00:00",
    postings_count: 2,
    last_posted_at: "2026-10-07T06:00:00+00:00",
    ...overrides,
  };
}

export const fakeRulePostings: readonly RulePostingView[] = [
  {
    proposal_id: "p-10",
    rule_id: "rule-1",
    date: "2026-10-06",
    amount: "70.27",
    counterparty: "KPN B.V.",
    account_code: "4500",
    posted_at: "2026-10-07T06:00:00+00:00",
    undoable: true,
  },
  {
    proposal_id: "p-11",
    rule_id: "rule-1",
    date: "2026-09-06",
    amount: "70.27",
    counterparty: "KPN B.V.",
    account_code: "4500",
    posted_at: "2026-09-07T06:00:00+00:00",
    undoable: false,
  },
];

/** The fake's ordered client list for `/next`: adm-2 → adm-1 → adm-3, then the end. */
const NEXT_ORDER: readonly FirmClientRef[] = [
  { administration_id: "adm-2", display_name: "Eva Mulder Design" },
  { administration_id: "adm-1", display_name: "Bakkerij Jansen" },
  { administration_id: "adm-3", display_name: "Hoveniersbedrijf De Linde" },
];

export function fakeNextClient(after: string): FirmNextClientView {
  const index = NEXT_ORDER.findIndex((entry) => entry.administration_id === after);
  const next = NEXT_ORDER[index + 1];
  if (next === undefined) return { administration_id: null, display_name: null, remaining: 0 };
  return { ...next, remaining: NEXT_ORDER.length - (index + 1) };
}

export interface FakeFirmApi extends FirmApiShape {
  readonly calls: { method: string; args: unknown[] }[];
}

export function fakeFirmApi(overrides: Partial<FirmApiShape> = {}): FakeFirmApi {
  const calls: { method: string; args: unknown[] }[] = [];
  const record =
    <A extends unknown[], R>(method: string, impl: (...args: A) => Promise<R>) =>
    (...args: A): Promise<R> => {
      calls.push({ method, args });
      return impl(...args);
    };

  // Server-side state the wave-2 fakes keep, so create/rename/archive/retire read back.
  let views: FirmSavedView[] = [
    {
      id: "view-1",
      name: "BTW maand",
      query: {
        chip: "vat_not_filed",
        q: "",
        assigned: "any",
        sort: "vat_due",
        dir: "asc",
        vat_frequency: "monthly",
      },
      count: 1,
    },
  ];
  let rules: BookingRuleView[] = [
    fakeRule(),
    fakeRule({
      id: "rule-2",
      counterparty_key: "shell",
      counterparty_label: "Shell",
      account_code: "4310",
      account_name: "Brandstof",
      max_amount: "150.00",
      status: "suspended",
      suspended_reason: "The person who made this rule can no longer reconcile for this client.",
      postings_count: 0,
      last_posted_at: null,
    }),
  ];
  let postings: RulePostingView[] = [...fakeRulePostings];
  let chase: ChaseSettingView = { enabled: false, cadence: "weekly" };
  let nextViewId = 2;

  const base: FirmApiShape = {
    getSummary: async () => fakeSummary,
    markSeen: async () => undefined,
    getWorklist: async (query) => fakeWorklist(query),
    snooze: async () => undefined,
    assign: async (ids) => ({ assigned: ids.length, failed: [] }),
    listStaff: async () => [...fakeStaff],
    getDeadlines: async () => [...fakeDeadlines],
    listProposals: async () => fakeProposals,
    decideProposals: async (decisions) => {
      // Like the server (decision 1): one rule per administration among the remembered approvals.
      const remembered = new Set(
        decisions
          .filter((entry) => entry.decision === "approve" && entry.remember === true)
          .map((entry) => adminOfProposal(entry.proposalId)),
      );
      return {
        approved: decisions.filter((entry) => entry.decision === "approve").length,
        rejected: decisions.filter((entry) => entry.decision === "reject").length,
        failed: [],
        rules_created: remembered.size,
      };
    },
    getInbox: async (options) => fakeInboxFor(options?.awaiting),
    previewChase: async (ids) => ({
      items: fakeChasePreview.items.filter((item) => ids.includes(item.administration_id)),
    }),
    sendChase: async (ids) => {
      const items = fakeChasePreview.items.filter((item) => ids.includes(item.administration_id));
      const blocked = items.filter((item) => item.blocked_reason !== null);
      return {
        sent: items.length - blocked.length,
        skipped: blocked.map((item) => ({
          administration_id: item.administration_id,
          reason: item.blocked_reason ?? "",
        })),
      };
    },
    listViews: async () => views.map((view) => ({ ...view })),
    createView: async (name, query) => {
      const view: FirmSavedView = { id: `view-${nextViewId++}`, name, query, count: 2 };
      views = [...views, view];
      return view;
    },
    renameView: async (id, name) => {
      views = views.map((view) => (view.id === id ? { ...view, name } : view));
    },
    archiveView: async (id) => {
      views = views.filter((view) => view.id !== id);
    },
    nextClient: async (after) => fakeNextClient(after),
    listRules: async () => rules.map((rule) => ({ ...rule })),
    retireRule: async (_administrationId, ruleId) => {
      rules = rules.map((rule) => (rule.id === ruleId ? { ...rule, status: "retired" } : rule));
    },
    setRuleMaxAmount: async (_administrationId, ruleId, maxAmount) => {
      rules = rules.map((rule) => (rule.id === ruleId ? { ...rule, max_amount: maxAmount } : rule));
    },
    listRulePostings: async () => postings.map((posting) => ({ ...posting })),
    undoRulePosting: async (_administrationId, proposalId) => {
      postings = postings.filter((posting) => posting.proposal_id !== proposalId);
    },
    getChaseSetting: async () => ({ ...chase }),
    setChaseSetting: async (_administrationId, setting) => {
      chase = { ...setting };
      return { ...chase };
    },
    ...overrides,
  };

  return {
    calls,
    getSummary: record("getSummary", base.getSummary),
    markSeen: record("markSeen", base.markSeen),
    getWorklist: record("getWorklist", base.getWorklist),
    snooze: record("snooze", base.snooze),
    assign: record("assign", base.assign),
    listStaff: record("listStaff", base.listStaff),
    getDeadlines: record("getDeadlines", base.getDeadlines),
    listProposals: record("listProposals", base.listProposals),
    decideProposals: record("decideProposals", base.decideProposals),
    getInbox: record("getInbox", base.getInbox),
    previewChase: record("previewChase", base.previewChase),
    sendChase: record("sendChase", base.sendChase),
    listViews: record("listViews", base.listViews),
    createView: record("createView", base.createView),
    renameView: record("renameView", base.renameView),
    archiveView: record("archiveView", base.archiveView),
    nextClient: record("nextClient", base.nextClient),
    listRules: record("listRules", base.listRules),
    retireRule: record("retireRule", base.retireRule),
    setRuleMaxAmount: record("setRuleMaxAmount", base.setRuleMaxAmount),
    listRulePostings: record("listRulePostings", base.listRulePostings),
    undoRulePosting: record("undoRulePosting", base.undoRulePosting),
    getChaseSetting: record("getChaseSetting", base.getChaseSetting),
    setChaseSetting: record("setChaseSetting", base.setChaseSetting),
  };
}

function adminOfProposal(proposalId: string): string {
  for (const group of fakeProposals.groups) {
    const found = group.proposals.find((proposal) => proposal.id === proposalId);
    if (found !== undefined) return found.administration_id;
  }
  return proposalId;
}

/** The mutating methods: none may run on mount (the StrictMode test). */
export const FIRM_MUTATIONS: readonly string[] = [
  "markSeen",
  "snooze",
  "assign",
  "decideProposals",
  "sendChase",
  "createView",
  "renameView",
  "archiveView",
  "retireRule",
  "setRuleMaxAmount",
  "undoRulePosting",
  "setChaseSetting",
];
