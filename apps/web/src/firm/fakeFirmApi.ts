/**
 * An in-memory `FirmApiShape` for tests (and for opening the screen without a backend): the
 * contract's own example shapes (docs/firm-home/contract.md), with every call recorded so a test
 * can assert what was sent. Any method can be replaced through `overrides`, which is how a test
 * makes one panel fail while the rest of the page answers.
 *
 * Not imported by the app itself, so it never reaches the production bundle.
 */

import type {
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

  const base: FirmApiShape = {
    getSummary: async () => fakeSummary,
    markSeen: async () => undefined,
    getWorklist: async (query) => fakeWorklist(query),
    snooze: async () => undefined,
    assign: async (ids) => ({ assigned: ids.length, failed: [] }),
    listStaff: async () => [...fakeStaff],
    getDeadlines: async () => [...fakeDeadlines],
    listProposals: async () => fakeProposals,
    decideProposals: async (decisions) => ({
      approved: decisions.filter((entry) => entry.decision === "approve").length,
      rejected: decisions.filter((entry) => entry.decision === "reject").length,
      failed: [],
    }),
    getInbox: async (options) => fakeInboxFor(options?.awaiting),
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
  };
}
