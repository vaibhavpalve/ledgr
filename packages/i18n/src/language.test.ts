/**
 * IAM-010g / FR-ONB-000 / FR-LOC-001b: which language a person gets, and in
 * what order the answers are consulted.
 */

import { describe, expect, it } from "vitest";

import {
  DEFAULT_LANGUAGE,
  SUPPORTED_LANGUAGES,
  isLanguage,
  primarySubtag,
  resolveLanguage,
} from "./language";
import { LANGUAGE_STORAGE_KEY, readStoredLanguage, storeLanguage } from "./device";

describe("resolveLanguage", () => {
  it("prefers the account setting over everything on the device", () => {
    // FR-LOC-001b makes language a property of the PERSON. Someone signing in
    // on a colleague's laptop, or on a machine in a Dutch office whose
    // browser is Dutch, gets their own language — otherwise "a per-user
    // setting" is really a per-device one that the account happens to seed.
    expect(
      resolveLanguage({
        accountLanguage: "en",
        storedChoice: "nl",
        preferredLanguages: ["nl-NL"],
        hostname: "app.ledgr.nl",
      }),
    ).toBe("en");
  });

  it("prefers an explicit device choice over the browser's languages", () => {
    // The device choice IS the person overriding their browser, on the
    // pre-login screen. Letting the browser win would make the control on
    // that screen do nothing on the next visit.
    expect(resolveLanguage({ storedChoice: "en", preferredLanguages: ["nl-NL", "nl"] })).toBe("en");
  });

  it("takes the browser's first supported language, ignoring the ones it cannot serve", () => {
    expect(resolveLanguage({ preferredLanguages: ["fr-FR", "de-DE", "en-GB"] })).toBe("en");
  });

  it("matches on the primary subtag, so a regional variant still resolves", () => {
    // A Flemish user's browser says nl-BE. Refusing to match it and falling
    // through to the hostname rule would be an accident of string equality.
    expect(resolveLanguage({ preferredLanguages: ["nl-BE"] })).toBe("nl");
    expect(resolveLanguage({ preferredLanguages: ["en-US"] })).toBe("en");
  });

  it("falls back to Dutch for .nl traffic, but only below the browser", () => {
    // §20's rule, and its ordering. Someone arriving at a .nl domain with an
    // English browser asked for English; the domain does not know better than
    // they do.
    expect(resolveLanguage({ hostname: "app.ledgr.nl" })).toBe("nl");
    expect(resolveLanguage({ hostname: "APP.LEDGR.NL" })).toBe("nl");
    expect(resolveLanguage({ preferredLanguages: ["en-GB"], hostname: "app.ledgr.nl" })).toBe("en");
  });

  // Not asserted here: that `ledgr.nl.example.com` is NOT treated as Dutch
  // traffic. The rule is a suffix match (`endsWith(".nl")`) and is written
  // that way deliberately, but while DEFAULT_LANGUAGE is also `nl` the two
  // paths produce the same answer and a test of it could not fail. Answering
  // PRD Q12 the other way is what would make it assertable.

  it("lands on the default when nothing answers", () => {
    expect(resolveLanguage({})).toBe(DEFAULT_LANGUAGE);
    expect(resolveLanguage({ preferredLanguages: ["fr-FR"], hostname: "ledgr.com" })).toBe(
      DEFAULT_LANGUAGE,
    );
  });

  it("ignores a stored or account value that is not a language we ship", () => {
    // A tampered localStorage entry, or a column value from a future release
    // rolled back. Fail to the resolution chain rather than to a crash or to
    // a language code the catalogue has no strings for.
    expect(resolveLanguage({ storedChoice: "de", preferredLanguages: ["en"] })).toBe("en");
    expect(resolveLanguage({ accountLanguage: "", preferredLanguages: ["en"] })).toBe("en");
  });
});

describe("the supported set", () => {
  it("is exactly Dutch and English, with neither listed as the primary one", () => {
    // FR-LOC-001. The array's order is what a picker renders, so it is
    // alphabetical by endonym rather than "the real language, then the
    // translation".
    expect([...SUPPORTED_LANGUAGES]).toEqual(["en", "nl"]);
  });

  it("accepts only those two", () => {
    expect(isLanguage("nl")).toBe(true);
    expect(isLanguage("en")).toBe(true);
    expect(isLanguage("de")).toBe(false);
    expect(isLanguage("NL")).toBe(false);
    expect(isLanguage(null)).toBe(false);
    expect(isLanguage(undefined)).toBe(false);
  });
});

describe("primarySubtag", () => {
  it("handles the separators an Accept-Language header actually carries", () => {
    expect(primarySubtag("nl-NL")).toBe("nl");
    expect(primarySubtag("nl_BE")).toBe("nl");
    expect(primarySubtag("  EN-gb  ")).toBe("en");
    expect(primarySubtag("nl")).toBe("nl");
  });
});

describe("device storage", () => {
  function fakeStore(initial: Record<string, string> = {}) {
    const data = new Map(Object.entries(initial));
    return {
      data,
      getItem: (key: string) => data.get(key) ?? null,
      setItem: (key: string, value: string) => void data.set(key, value),
    };
  }

  it("round-trips a choice under the documented key", () => {
    const store = fakeStore();
    expect(storeLanguage("en", store)).toBe(true);
    expect(store.data.get(LANGUAGE_STORAGE_KEY)).toBe("en");
    expect(readStoredLanguage(store)).toBe("en");
  });

  it("reads nothing rather than something for an unrecognised stored value", () => {
    expect(readStoredLanguage(fakeStore({ [LANGUAGE_STORAGE_KEY]: "de" }))).toBeNull();
    expect(readStoredLanguage(fakeStore())).toBeNull();
  });

  it("survives storage that throws, because the login screen must still render", () => {
    // A browser configured to block site data throws on ACCESS, not on write
    // — so an unguarded read takes down the pre-login screen, which is the
    // one screen that has to work for everybody (IAM-010g).
    const hostile = {
      getItem() {
        throw new Error("SecurityError: access is denied for this document");
      },
      setItem() {
        throw new Error("SecurityError: access is denied for this document");
      },
    };
    expect(readStoredLanguage(hostile)).toBeNull();
    expect(storeLanguage("nl", hostile)).toBe(false);
  });

  it("reports honestly when there is nowhere to remember the choice", () => {
    expect(storeLanguage("nl", null)).toBe(false);
    expect(readStoredLanguage(null)).toBeNull();
  });
});
