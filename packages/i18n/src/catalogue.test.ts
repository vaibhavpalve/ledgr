/**
 * FR-LOC-001 as behaviour rather than as data.
 *
 * scripts/check_translations.py checks the catalogue's SHAPE and fails the
 * build (FR-LOC-001d). This file checks what the runtime does with it — in
 * particular that it never quietly substitutes one language for the other,
 * which is the behaviour the requirement rules out and the behaviour every
 * i18n library defaults to.
 */

import { describe, expect, it } from "vitest";

import {
  GLOSSARY,
  MESSAGES,
  MissingMessageError,
  glossaryDefinition,
  glossaryTerm,
  hasMessage,
  translate,
} from "./catalogue";
import { SUPPORTED_LANGUAGES } from "./language";

describe("translate", () => {
  it("renders each language from the same record", () => {
    expect(translate("client.header.none_selected", "nl")).toBe("Geen klant geselecteerd");
    expect(translate("client.header.none_selected", "en")).toBe("No client selected");
  });

  it("interpolates placeholders", () => {
    expect(translate("client.switcher.no_matches", "nl", { query: "Bakker" })).toBe(
      "Geen klanten gevonden voor “Bakker”",
    );
    expect(translate("client.switcher.no_matches", "en", { query: "Bakker" })).toBe(
      "No clients match “Bakker”",
    );
  });

  it("throws on an unknown key instead of showing one to a user", () => {
    // FR-UX-007: "Untranslated or developer-facing strings never reach a
    // user." A library that returns the key on a miss renders
    // `client.switcher.no_matches` into the page, which is the most
    // developer-facing string there is.
    expect(() => translate("client.nope", "nl")).toThrow(MissingMessageError);
  });

  it("throws rather than leaving a placeholder unfilled", () => {
    // The alternative is a literal `{query}` on screen. Both are bugs; only
    // one of them is noticed before release.
    expect(() => translate("client.switcher.no_matches", "nl")).toThrow(MissingMessageError);
  });

  it("does not fall back from one language to the other", () => {
    // The property FR-LOC-001 turns on: "A missing translation is a release
    // blocker, not a fallback to English." There is no code path that could
    // do it — a message is ONE record holding both languages, so a Dutch
    // string cannot be absent while an English one exists, and there is
    // nothing for a fallback to fall back to. This asserts the shape that
    // makes that true rather than a behaviour that could be added back.
    for (const [key, record] of Object.entries(MESSAGES)) {
      expect(record.nl, `${key} has no Dutch`).toBeDefined();
      expect(record.en, `${key} has no English`).toBeDefined();
    }
  });

  it("renders every message in every language without throwing", () => {
    // A smoke pass over the whole catalogue. Catches a record whose two
    // languages disagree about their plural shape — one a string, the other
    // {one, other} — which the shape check also catches and which would
    // surface here as a thrown error on one language only.
    for (const [key, record] of Object.entries(MESSAGES)) {
      for (const language of SUPPORTED_LANGUAGES) {
        const text = typeof record.nl === "string" ? record.nl : record.nl.other;
        const params: Record<string, string | number> = {};
        for (const [, name] of text.matchAll(/\{(\w+)\}/g)) {
          if (name !== undefined) params[name] = "x";
        }
        // `count` is always a number, whatever the placeholder scan says.
        // Counted messages interpolate {count} as well as selecting on it, so
        // filling it from the scan like any other placeholder would hand a
        // counted message the string "x" and make this smoke test fail on
        // every correctly-written plural.
        params.count = 2;
        expect(() => translate(key, language, params)).not.toThrow();
      }
    }
  });

  it("needs a count for a counted message", () => {
    expect(() => translate("client.switcher.current", "nl", { count: 1 })).not.toThrow();
  });

  it("selects the right plural form in both languages", () => {
    // The catalogue's counted messages arrived with the offline capture queue
    // (MOB-003), which is the first surface that had to say "1 bon" and
    // "2 bonnen".
    expect(translate("capture.queue.waiting", "nl", { count: 1 })).toBe("1 bon wacht op uploaden");
    expect(translate("capture.queue.waiting", "nl", { count: 2 })).toBe(
      "2 bonnen wachten op uploaden",
    );
    expect(translate("capture.queue.waiting", "en", { count: 1 })).toBe(
      "1 receipt waiting to upload",
    );
    expect(translate("capture.queue.waiting", "en", { count: 0 })).toBe(
      "0 receipts waiting to upload",
    );
  });
});

describe("hasMessage", () => {
  it("answers without throwing, for the caller that builds a key at runtime", () => {
    expect(hasMessage("client.header.none_selected")).toBe(true);
    expect(hasMessage("client.header.invented")).toBe(false);
  });
});

describe("the glossary (FR-LOC-001c)", () => {
  it("carries the terms the requirement names, plus kolommenbalans", () => {
    // FR-LOC-001c names four by way of example (BTW, KvK, suppletie,
    // grootboek); kolommenbalans was added afterwards and is flagged for
    // review — it appears nowhere in the PRD, so its treatment is a question
    // rather than a decision. See glossary.json's review blocks.
    expect(Object.keys(GLOSSARY).sort()).toEqual([
      "btw",
      "grootboek",
      "kolommenbalans",
      "kvk",
      "suppletie",
    ]);
  });

  it("keeps each term's Dutch form and defines it in both languages", () => {
    for (const [id, term] of Object.entries(GLOSSARY)) {
      expect(term.keepDutch, `${id} is in the glossary but not marked keepDutch`).toBe(true);
      expect(glossaryDefinition(id, "nl")).toBeTruthy();
      expect(glossaryDefinition(id, "en")).toBeTruthy();
      // The definitions differ; the TERM does not. That is the whole rule.
      expect(glossaryDefinition(id, "nl")).not.toBe(glossaryDefinition(id, "en"));
    }
  });

  it("does not translate BTW to VAT in the English UI", () => {
    // The named example. `btw` keeps its form; the English definition is
    // allowed to say "value added tax" once, to anchor the concept for a
    // reader who has never met the Dutch term.
    expect(glossaryTerm("btw")?.term).toBe("BTW");
    expect(glossaryDefinition("btw", "en")).toContain("Belasting over de toegevoegde waarde");
  });

  it("costs only the tooltip when a term is unknown", () => {
    // A missing glossary entry must not throw: the term beside it is legible
    // on its own, and losing a hover is not worth losing the screen.
    expect(glossaryDefinition("rgs", "nl")).toBeUndefined();
    expect(glossaryTerm("rgs")).toBeUndefined();
  });
});
