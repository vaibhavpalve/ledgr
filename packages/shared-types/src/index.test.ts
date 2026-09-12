/**
 * `CLIENT_COLOURS` says of itself, in index.ts:
 *
 *   "Mirrors client_colour (migration 0018) and api.firm.switcher.PALETTE."
 *
 * A comment claiming three things agree is worth exactly as much as a check
 * that they do. This is that check.
 *
 * --- Why it matters that the ORDER matches too ---
 *
 * `client_colour.position` is the tie-break the colour-allocation trigger in
 * migration 0018 uses, so the sequence is not decoration: two lists holding
 * the same ten tokens in different orders would still disagree about which
 * colour a new client gets. The assertions below compare sequences, not sets.
 *
 * --- Why the palette is checked and the types are not ---
 *
 * Everything else this package exports is a TYPE, and types are checked by
 * `tsc --noEmit` wherever they are used - a runtime test could only restate
 * them. `CLIENT_COLOURS` is the one runtime VALUE here, and it is a copy of
 * data that lives in two other languages, which is exactly the kind of thing
 * that drifts silently.
 *
 * This is the same device packages/i18n/formatting-cases.json uses for
 * formatting and tests/ledger/fiscal_cases.py uses for period derivation:
 * where one fact necessarily exists more than once, compare the copies rather
 * than trusting them.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { CLIENT_COLOURS, FONT_CATALOGUE } from "./index";

function read(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf-8");
}

/** The `insert into client_colour ... values ('indigo', 1), ...` block. */
function paletteFromMigration(): string[] {
  const sql = read("../../../apps/api/migrations/0018_client_switcher.sql");
  const block = /insert into client_colour[\s\S]*?;/i.exec(sql);
  if (block === null) {
    throw new Error("no client_colour seed found in migration 0018");
  }
  return [...block[0].matchAll(/\('(\w+)'\s*,\s*(\d+)\)/g)]
    .map((match) => ({ token: match[1] ?? "", position: Number(match[2]) }))
    .sort((a, b) => a.position - b.position)
    .map((entry) => entry.token);
}

/** `PALETTE: tuple[str, ...] = (...)` in api.firm.switcher. */
function paletteFromApi(): string[] {
  const source = read("../../../apps/api/src/api/firm/switcher.py");
  const block = /PALETTE:\s*tuple\[str, \.\.\.\]\s*=\s*\(([\s\S]*?)\)/.exec(source);
  if (block === null) {
    throw new Error("no PALETTE found in api/firm/switcher.py");
  }
  return [...(block[1] ?? "").matchAll(/"(\w+)"/g)].map((match) => match[1] ?? "");
}

describe("the client colour palette", () => {
  it("finds all three definitions", () => {
    // Without this, a regex that stopped matching would leave two empty
    // arrays comparing equal and the suite passing while checking nothing.
    expect(CLIENT_COLOURS).toHaveLength(10);
    expect(paletteFromMigration()).toHaveLength(10);
    expect(paletteFromApi()).toHaveLength(10);
  });

  it("matches migration 0018's client_colour table, in position order", () => {
    expect([...CLIENT_COLOURS]).toEqual(paletteFromMigration());
  });

  it("matches api.firm.switcher.PALETTE, in order", () => {
    expect([...CLIENT_COLOURS]).toEqual(paletteFromApi());
  });

  it("has no duplicates, which would make two clients indistinguishable", () => {
    expect(new Set(CLIENT_COLOURS).size).toBe(CLIENT_COLOURS.length);
  });
});

/** The `insert into template_font (...) values (...), ...;` block. */
function fontCatalogueFromMigration(): { code: string; family_name: string; category: string }[] {
  const sql = read("../../../apps/api/migrations/0042_invoice_templates.sql");
  const block = /insert into template_font[\s\S]*?;/i.exec(sql);
  if (block === null) {
    throw new Error("no template_font seed found in migration 0042");
  }
  return [...block[0].matchAll(/\('(\w+)',\s*'([^']+)',\s*'(\w+)'\)/g)].map((match) => ({
    code: match[1] ?? "",
    family_name: match[2] ?? "",
    category: match[3] ?? "",
  }));
}

/** `FONT_CODES: frozenset[str] = frozenset({...})` in api.templates.model. */
function fontCodesFromApi(): string[] {
  const source = read("../../../apps/api/src/api/templates/model.py");
  const block = /FONT_CODES: frozenset\[str\] = frozenset\(\s*\{([\s\S]*?)\}\s*\)/.exec(source);
  if (block === null) {
    throw new Error("no FONT_CODES found in api/templates/model.py");
  }
  return [...(block[1] ?? "").matchAll(/"(\w+)"/g)].map((match) => match[1] ?? "");
}

describe("the curated font catalogue — FR-TPL-002", () => {
  it("finds both other definitions", () => {
    // Same guard as the client-colour suite above: without this, a regex
    // that stopped matching would leave empty arrays comparing equal and the
    // suite passing while checking nothing.
    expect(FONT_CATALOGUE).toHaveLength(9);
    expect(fontCatalogueFromMigration()).toHaveLength(9);
    expect(fontCodesFromApi()).toHaveLength(9);
  });

  it("matches migration 0042's template_font seed rows exactly — code, family name and category, in order", () => {
    expect([...FONT_CATALOGUE]).toEqual(fontCatalogueFromMigration());
  });

  it("matches api.templates.model.FONT_CODES as a set", () => {
    const codes = FONT_CATALOGUE.map((font) => font.code);
    expect(new Set(codes)).toEqual(new Set(fontCodesFromApi()));
  });

  it("has no duplicate codes", () => {
    const codes = FONT_CATALOGUE.map((font) => font.code);
    expect(new Set(codes).size).toBe(codes.length);
  });

  it("only uses the three categories 0042's CHECK constraint allows", () => {
    for (const font of FONT_CATALOGUE) {
      expect(["serif", "sans", "mono"]).toContain(font.category);
    }
  });
});
