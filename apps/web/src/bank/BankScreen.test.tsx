import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@ledgr/i18n";
import type {
  BankAccountView,
  BankMatchCandidateView,
  BankTransactionView,
} from "@ledgr/shared-types";

import { ApiError } from "../api/http";
import { BankScreen } from "./BankScreen";

/**
 * FR-BNK-003/004 (ADR-091): each unmatched incoming line shows its best open invoice and how sure
 * that is; only the certain ones are matched in bulk.
 */

const account: BankAccountView = {
  id: "ba-1",
  name: "Zakelijke rekening",
  iban: "NL91ABNA0417164300",
  currency: "EUR",
  ledger_account_id: "la-1100",
  status: "active",
};

function suggestion(
  invoiceId: string,
  confidence: BankMatchCandidateView["confidence"],
  reasons: BankMatchCandidateView["reasons"],
): BankMatchCandidateView {
  return {
    invoice_id: invoiceId,
    invoice_reference: `2026-${invoiceId}`,
    customer_name: "Hotel De Gouden Leeuw B.V.",
    outstanding: "1149.50",
    invoice_date: "2026-09-01",
    confidence,
    reasons,
  };
}

function line(id: string, match: BankMatchCandidateView | null): BankTransactionView {
  return {
    id,
    bank_account_id: "ba-1",
    booking_date: "2026-09-22",
    value_date: null,
    amount: "1149.50",
    currency: "EUR",
    counterparty_name: "HOTEL DE GOUDEN LEEUW BV",
    counterparty_iban: null,
    description: "factuur",
    status: "unmatched",
    matched_sales_invoice_id: null,
    journal_entry_id: null,
    reconciled_at: null,
    suggestion: match,
  };
}

const certainA = line("t1", suggestion("0007", "high", ["reference"]));
const certainB = line("t2", suggestion("0008", "high", ["name", "only_candidate"]));
const likely = line("t3", suggestion("0009", "medium", ["name"]));
const nothing = line("t4", null);

const services = {
  bank: {
    listAccounts: vi.fn(async () => [account]),
    listTransactions: vi.fn(async () => [certainA, certainB, likely, nothing]),
    matchCandidates: vi.fn(async () => []),
    reconcileWithInvoice: vi.fn(async (_admin: string, transactionId: string) => ({
      ...line(transactionId, null),
      status: "reconciled" as const,
    })),
    reconcileGeneric: vi.fn(),
    importStatement: vi.fn(),
    createAccount: vi.fn(),
  },
  ledger: {
    listChartOfAccounts: vi.fn(async () => []),
  },
};

vi.mock("../session/SessionProvider", () => ({
  useAdministration: () => ({ administration: { id: "adm-A" } }),
}));
vi.mock("../session/ServicesProvider", () => ({ useServices: () => services }));

function open() {
  render(
    <I18nProvider initialLanguage="en">
      <BankScreen />
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("suggested matches", () => {
  it("shows each line's best invoice and how sure it is", async () => {
    open();
    const first = await screen.findByTestId("bank-suggestion-t1");
    expect(first.textContent).toContain("Certain");
    expect(first.textContent).toContain("Invoice 2026-0007 to Hotel De Gouden Leeuw B.V.");
    expect(screen.getByTestId("bank-suggestion-t3").textContent).toContain("Likely");
    expect(screen.queryByTestId("bank-suggestion-t4")).toBeNull();
  });

  it("matches only the certain lines in bulk", async () => {
    open();
    const banner = await screen.findByTestId("bank-certain");
    expect(banner.textContent).toContain("2 payments certainly match an open invoice.");

    fireEvent.click(within(banner).getByTestId("bank-match-certain"));

    await waitFor(() =>
      expect(screen.getByTestId("bank-match-result").textContent).toContain(
        "2 payments matched and booked as received.",
      ),
    );
    expect(services.bank.reconcileWithInvoice.mock.calls).toEqual([
      ["adm-A", "t1", "0007"],
      ["adm-A", "t2", "0008"],
    ]);
  });

  it("reports the certain matches that were refused and carries on", async () => {
    services.bank.reconcileWithInvoice.mockRejectedValueOnce(
      new ApiError(409, "errors.bank_transaction_already_reconciled", "already"),
    );
    open();
    fireEvent.click(await screen.findByTestId("bank-match-certain"));

    await waitFor(() =>
      expect(screen.getByTestId("bank-match-result").textContent).toContain(
        "1 payment matched; 1 failed, please review those yourself.",
      ),
    );
    expect(services.bank.reconcileWithInvoice).toHaveBeenCalledTimes(2);
  });

  it("books nothing to an account the person did not choose", async () => {
    services.ledger.listChartOfAccounts.mockResolvedValueOnce([
      { id: "la-0200", code: "0200", name: "Machines", account_type: "asset", status: "active" },
      {
        id: "la-4400",
        code: "4400",
        name: "Kantoorkosten",
        account_type: "expense",
        status: "active",
      },
    ] as never);
    open();
    fireEvent.click(await screen.findByTestId("bank-reconcile-open-t4"));
    const confirm = (await screen.findByTestId("bank-reconcile-confirm-t4")) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("bank-offset-account-t4"), {
      target: { value: "la-4400" },
    });
    expect(confirm.disabled).toBe(false);
  });

  it("matches a likely line with one click", async () => {
    open();
    fireEvent.click(await screen.findByTestId("bank-suggestion-t3-match"));
    await waitFor(() =>
      expect(services.bank.reconcileWithInvoice).toHaveBeenCalledWith("adm-A", "t3", "0009"),
    );
  });
});
