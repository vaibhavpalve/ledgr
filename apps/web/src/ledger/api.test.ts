import { describe, expect, it } from "vitest";

import { fakeFetch, jsonResponse } from "../testing/fakeFetch";
import { LedgerApi, sumDecimals } from "./api";

const rows = [
  {
    account_id: "a1",
    account_code: "1100",
    account_name: "Bank",
    account_type: "asset",
    total_debit: "1240.00",
    total_credit: "0.00",
    balance: "1240.00",
  },
  {
    account_id: "a2",
    account_code: "1600",
    account_name: "Crediteuren",
    account_type: "liability",
    total_debit: "0.00",
    total_credit: "1240.00",
    balance: "-1240.00",
  },
];

describe("LedgerApi", () => {
  it("reads the trial balance for one fiscal year and sums totals as strings when the server sent none", async () => {
    const { impl, calls } = fakeFetch({
      "GET /v1/administrations/adm-A/trial-balance": () => jsonResponse(rows),
    });
    const api = new LedgerApi({ language: () => "nl", fetchImpl: impl });

    const balance = await api.getTrialBalance("adm-A", "fy-1");

    expect(calls[0]?.url).toBe("/v1/administrations/adm-A/trial-balance?fiscal_year_id=fy-1");
    expect(balance.rows).toHaveLength(2);
    expect(balance.total_debit).toBe("1240.00");
    expect(balance.total_credit).toBe("1240.00");
  });

  it("keeps the server's totals when it sent them", async () => {
    const api = new LedgerApi({
      language: () => "nl",
      fetchImpl: fakeFetch({
        "GET /v1/administrations/adm-A/trial-balance": () =>
          jsonResponse({ fiscal_year_id: "fy-1", rows, total_debit: "9.99", total_credit: "9.99" }),
      }).impl,
    });

    const balance = await api.getTrialBalance("adm-A", "fy-1");

    expect(balance.total_debit).toBe("9.99");
  });

  it("pages journal entries by cursor", async () => {
    const entry = {
      id: "e1",
      entry_number: 7,
      entry_date: "2026-01-04",
      description: "Dakgoot vervangen",
      journal_code: "B",
      document_reference: null,
      reverses_entry_id: null,
      total: "1240.00",
    };
    const { impl, calls } = fakeFetch({
      "GET /v1/administrations/adm-A/journal-entries": () =>
        jsonResponse({ entries: [entry], next_cursor: "abc" }),
    });
    const api = new LedgerApi({ language: () => "nl", fetchImpl: impl });

    const page = await api.listJournalEntries("adm-A", {
      fiscalYearId: "fy-1",
      cursor: "xyz",
      limit: 50,
    });

    expect(calls[0]?.url).toBe(
      "/v1/administrations/adm-A/journal-entries?fiscal_year_id=fy-1&cursor=xyz&limit=50",
    );
    expect(page.entries).toEqual([entry]);
    expect(page.next_cursor).toBe("abc");
  });

  it("accepts a chart as a bare array or wrapped in `accounts`", async () => {
    const account = {
      id: "a1",
      code: "1100",
      name: "Bank",
      account_type: "asset",
      status: "active",
      rgs_code: "BLimBanRba",
      control_kind: null,
    };
    const bare = new LedgerApi({
      language: () => "nl",
      fetchImpl: fakeFetch({
        "GET /v1/administrations/adm-A/chart-of-accounts": () => jsonResponse([account]),
      }).impl,
    });
    const wrapped = new LedgerApi({
      language: () => "nl",
      fetchImpl: fakeFetch({
        "GET /v1/administrations/adm-A/chart-of-accounts": () =>
          jsonResponse({ accounts: [account] }),
      }).impl,
    });

    expect(await bare.listChartOfAccounts("adm-A")).toEqual(
      await wrapped.listChartOfAccounts("adm-A"),
    );
  });
});

describe("sumDecimals — exact, string in, string out (NFR-031)", () => {
  it("adds without floating point", () => {
    expect(sumDecimals(["0.10", "0.20"])).toBe("0.30");
    expect(sumDecimals(["1234.56", "-1234.56"])).toBe("0.00");
    expect(sumDecimals(["12345678901234567.89", "0.01"])).toBe("12345678901234567.90");
  });

  it("refuses anything that is not a plain decimal", () => {
    expect(() => sumDecimals(["1e3"])).toThrow();
    expect(() => sumDecimals(["1,00"])).toThrow();
  });
});
