import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { I18nProvider } from "@ledgr/i18n";
import type {
  BankAccountView,
  BankFeedConnectionView,
  BankFeedStatusView,
} from "@ledgr/shared-types";

import { BankFeedReturn } from "./BankFeed";
import { BankScreen } from "./BankScreen";

/**
 * The live bank feed on the Bank screen (ADR-108): hidden without a provider, the connect flow
 * to the bank's own page, the linked state with Fetch now and Disconnect, the reason a connection
 * failed, and the return from the bank.
 */

const account: BankAccountView = {
  id: "ba-1",
  name: "ABN AMRO",
  iban: "NL91ABNA0417164300",
  currency: "EUR",
  ledger_account_id: "la-1100",
  status: "active",
};

function connection(overrides: Partial<BankFeedConnectionView> = {}): BankFeedConnectionView {
  return {
    id: "conn-1",
    bank_account_id: "ba-1",
    provider: "gocardless",
    institution_id: "ABNAMRO_ABNANL2A",
    institution_name: "ABN AMRO",
    status: "linked",
    consent_expires_at: "2027-01-06T07:00:00+00:00",
    last_synced_at: "2026-10-08T07:00:00+00:00",
    last_error: null,
    ...overrides,
  };
}

const feed = (status: Partial<BankFeedStatusView>): BankFeedStatusView => ({
  configured: true,
  provider: "gocardless",
  connection: null,
  ...status,
});

const services = {
  bank: {
    listAccounts: vi.fn(async () => [account]),
    listTransactions: vi.fn(async () => []),
    getFeed: vi.fn(async (): Promise<BankFeedStatusView> => feed({})),
    listFeedInstitutions: vi.fn(async () => [
      { id: "ING_INGBNL2A", name: "ING", bic: "INGBNL2A", logo: null },
      { id: "ABNAMRO_ABNANL2A", name: "ABN AMRO", bic: "ABNANL2A", logo: null },
    ]),
    connectFeed: vi.fn(async () => ({
      connection: connection({ status: "pending" }),
      link: "https://bank.example/consent",
    })),
    completeFeed: vi.fn(async () => connection()),
    syncFeed: vi.fn(async () => ({
      connection: connection({ last_synced_at: "2026-10-09T07:00:00+00:00" }),
      imported: { import_id: "imp-1", transaction_count: 3, duplicate_count: 1 },
    })),
    disconnectFeed: vi.fn(async () => connection({ status: "revoked" })),
  },
  ledger: {
    listChartOfAccounts: vi.fn(async () => []),
  },
};

vi.mock("../session/SessionProvider", () => ({
  useAdministration: () => ({ administration: { id: "adm-A" } }),
}));
vi.mock("../session/ServicesProvider", () => ({ useServices: () => services }));

function open(route = "/bank") {
  render(
    <MemoryRouter initialEntries={[route]}>
      <I18nProvider initialLanguage="en">
        <Routes>
          <Route path="/bank" element={<BankScreen />} />
          <Route path="/bank/feed-return" element={<BankFeedReturn />} />
        </Routes>
      </I18nProvider>
    </MemoryRouter>,
  );
}

const assigned: string[] = [];
const originalLocation = window.location;

beforeEach(() => {
  vi.clearAllMocks();
  assigned.length = 0;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...originalLocation, assign: (url: string) => assigned.push(url) },
  });
});

afterEach(() => {
  Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
});

describe("without a provider", () => {
  it("shows no feed at all: statement import stays the way in", async () => {
    services.bank.getFeed.mockResolvedValueOnce(feed({ configured: false, provider: "none" }));
    open();
    await screen.findByTestId("bank-summary");
    await waitFor(() => expect(services.bank.getFeed).toHaveBeenCalled());
    expect(screen.queryByTestId("bank-feed")).toBeNull();
  });
});

describe("connecting", () => {
  it("lets the person pick their bank and sends them to its own page", async () => {
    open();
    fireEvent.click(await screen.findByTestId("bank-feed-connect"));
    const dialog = await screen.findByTestId("bank-feed-dialog");
    expect(dialog.getAttribute("role")).toBe("dialog");

    fireEvent.change(within(dialog).getByTestId("bank-feed-search"), { target: { value: "abn" } });
    const banks = within(dialog).getByTestId("bank-feed-banks");
    expect(within(banks).getAllByRole("button")).toHaveLength(1);

    fireEvent.click(within(dialog).getByTestId("bank-feed-bank-ABNAMRO_ABNANL2A"));
    await waitFor(() => expect(assigned).toEqual(["https://bank.example/consent"]));
    expect(services.bank.connectFeed).toHaveBeenCalledWith(
      "adm-A",
      "ba-1",
      { id: "ABNAMRO_ABNANL2A", name: "ABN AMRO" },
      "en",
    );
  });

  it("finishes on the return from the bank and opens Bank with a notice", async () => {
    open("/bank/feed-return?ref=conn-1");
    expect((await screen.findByTestId("bank-feed-notice")).textContent).toBe(
      "Connected to ABN AMRO. Transactions are fetched every day.",
    );
    expect(services.bank.completeFeed).toHaveBeenCalledTimes(1);
    expect(services.bank.completeFeed).toHaveBeenCalledWith("adm-A", "conn-1");
  });

  it("says why when the bank refused or the account did not match", async () => {
    services.bank.completeFeed.mockResolvedValueOnce(
      connection({ status: "failed", last_error: "bank_feed_account_mismatch" }),
    );
    open("/bank/feed-return?ref=conn-1");
    expect((await screen.findByTestId("bank-feed-notice")).textContent).toContain(
      "its IBAN does not match",
    );
  });
});

describe("a linked account", () => {
  beforeEach(() => {
    services.bank.getFeed.mockResolvedValue(feed({ connection: connection() }));
  });
  afterEach(() => {
    services.bank.getFeed.mockResolvedValue(feed({}));
  });

  it("shows it is live, when it last fetched and how long access lasts", async () => {
    open();
    expect((await screen.findByTestId("bank-feed-live")).textContent).toContain(
      "Live from your bank",
    );
    expect(screen.getByTestId("bank-feed-synced").textContent).toBe(
      "Last fetched 08-10-2026 · Access until 06-01-2027",
    );
  });

  it("fetches on request and reads the transactions again", async () => {
    open();
    fireEvent.click(await screen.findByTestId("bank-feed-sync"));
    expect((await screen.findByTestId("bank-feed-message")).textContent).toBe(
      "3 new transactions fetched.",
    );
    await waitFor(() =>
      expect(services.bank.listTransactions.mock.calls.length).toBeGreaterThan(1),
    );
  });

  it("disconnects and offers to connect again", async () => {
    open();
    fireEvent.click(await screen.findByTestId("bank-feed-disconnect"));
    expect((await screen.findByTestId("bank-feed-message")).textContent).toContain("Disconnected");
    expect(screen.getByTestId("bank-feed-connect").textContent).toContain("Connect your bank");
  });

  it("says when access has ended", async () => {
    services.bank.getFeed.mockResolvedValue(
      feed({
        connection: connection({ status: "expired", last_error: "bank_feed_consent_expired" }),
      }),
    );
    open();
    expect((await screen.findByTestId("bank-feed-reason")).textContent).toContain(
      "Access to your bank has ended",
    );
    expect(screen.getByTestId("bank-feed-connect").textContent).toContain("Connect again");
  });
});
