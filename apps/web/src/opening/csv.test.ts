import { describe, expect, it } from "vitest";

import { readTrialBalance } from "./csv";

describe("readTrialBalance", () => {
  it("reads a Dutch-Excel export with separate debit and credit columns", () => {
    const text = [
      "Grootboekrekening;Omschrijving;Debet;Credit",
      "1100;Bank;12.500,00;",
      "0500;Aandelenkapitaal;;10.000,00",
    ].join("\r\n");
    expect(readTrialBalance(text, "nl")).toEqual({
      lines: [
        { code: "1100", debit: "12500.00", credit: "0" },
        { code: "0500", debit: "0", credit: "10000.00" },
      ],
      unreadable: [],
    });
  });

  it("reads one signed balance column, positive as debit", () => {
    const text = 'code,name,balance\n1000,Kas,"250.50"\n1790,Lening,-1500\n';
    expect(readTrialBalance(text, "en")?.lines).toEqual([
      { code: "1000", debit: "250.50", credit: "0" },
      { code: "1790", debit: "0", credit: "1500" },
    ]);
  });

  it("reports a row it cannot read instead of guessing", () => {
    const text = "Rekening;Saldo\n1100;veel\n1000;10,00";
    expect(readTrialBalance(text, "nl")).toEqual({
      lines: [{ code: "1000", debit: "10.00", credit: "0" }],
      unreadable: ["1100"],
    });
  });

  it("gives up on a file with no code column", () => {
    expect(readTrialBalance("naam;bedrag\nBank;10", "nl")).toBeNull();
  });
});
