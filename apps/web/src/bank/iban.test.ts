import { describe, expect, it } from "vitest";

import { formatIban, isValidIban, normaliseIban } from "./iban";

describe("IBAN", () => {
  it("normalises what a person types into what is sent", () => {
    expect(normaliseIban(" nl91 abna-0417 1643 00 ")).toBe("NL91ABNA0417164300");
  });

  it("groups it in fours, as banks print it", () => {
    expect(formatIban("NL91ABNA0417164300")).toBe("NL91 ABNA 0417 1643 00");
    expect(formatIban("nl91abna")).toBe("NL91 ABNA");
  });

  it("accepts real IBANs, Dutch and foreign", () => {
    expect(isValidIban("NL91 ABNA 0417 1643 00")).toBe(true);
    expect(isValidIban("NL02ABNA0123456789")).toBe(true);
    expect(isValidIban("DE89 3704 0044 0532 0130 00")).toBe(true);
    expect(isValidIban("BE68 5390 0754 7034")).toBe(true);
  });

  it("refuses a typo in any digit, and a wrong length", () => {
    expect(isValidIban("NL91ABNA0417164301")).toBe(false);
    expect(isValidIban("NL19ABNA0417164300")).toBe(false);
    expect(isValidIban("NL91ABNA041716430")).toBe(false);
    expect(isValidIban("1234")).toBe(false);
    expect(isValidIban("")).toBe(false);
  });
});
