import { describe, expect, it } from "vitest";

import { toDecimalInput } from "./decimal";

describe("toDecimalInput", () => {
  it.each([
    ["95,00", "nl", "95.00"],
    ["95.00", "nl", "95.00"],
    ["1.234,56", "nl", "1234.56"],
    ["1,234.56", "en", "1234.56"],
    ["€ 121,00", "nl", "121.00"],
    ["1 250,5", "nl", "1250.5"],
    ["1.250", "nl", "1250"],
    ["1.250", "en", "1.250"],
    ["1.5", "nl", "1.5"],
    ["-42,10", "nl", "-42.10"],
    ["0,125", "nl", "0.125"],
    ["", "nl", ""],
  ])("%s (%s) is %s", (typed, language, expected) => {
    expect(toDecimalInput(typed, language)).toBe(expected);
  });

  it("leaves anything that is not a number as typed, for the server to name", () => {
    expect(toDecimalInput("abc", "nl")).toBe("abc");
    expect(toDecimalInput("1,2,3", "nl")).toBe("1,2,3");
  });
});
