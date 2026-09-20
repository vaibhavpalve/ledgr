import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const root = resolve(__dirname, "../../..");
const read = (path: string) => readFileSync(resolve(root, path), "utf8").replace(/\r\n/g, "\n");

describe("Ledgr UI handoff tokens (ADR-080)", () => {
  it("ui-tokens.css is a verbatim copy of design/tokens/tokens.css", () => {
    expect(read("packages/design-tokens/ui-tokens.css")).toBe(read("design/tokens/tokens.css"));
  });

  it("every font file ui-fonts.css names exists under public/fonts", () => {
    const css = read("packages/design-tokens/ui-fonts.css");
    const files = [...css.matchAll(/url\("\/fonts\/([^"]+)"\)/g)].map((m) => m[1]);
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      expect(() =>
        readFileSync(resolve(root, "apps/web/public/fonts", file as string)),
      ).not.toThrow();
    }
  });
});
