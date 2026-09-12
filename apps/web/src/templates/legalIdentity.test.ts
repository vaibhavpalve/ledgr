import { describe, expect, it } from "vitest";

import {
  FIXED_LEGAL_IDENTITY_TEXT,
  REQUIRED_TAG_LITERAL,
  composeLegalIdentityText,
  decomposeLegalIdentityText,
} from "./legalIdentity";

/**
 * FR-TPL-009's load-bearing guarantee for the `legal_identity` block: no
 * matter what a person types into the free-text field, what gets SENT to the
 * API always contains both statutory merge tags. `TemplateDesigner` never
 * lets a person edit the tags directly — this is the proof that the
 * concatenation behind that UI cannot be argued out of the guarantee either.
 */
const ADVERSARIAL_FREE_TEXT: readonly string[] = [
  "",
  "   ",
  "IBAN NL00 BANK 0123 4567 89, algemene voorwaarden op aanvraag.",
  // Already mentions the VAT tag in some other, unrelated way — the
  // guarantee is "contains", not "contains exactly once".
  `Ons btw-nummer staat hierboven, maar voor de zekerheid: ${REQUIRED_TAG_LITERAL.vat}`,
  // Both tags already present, out of order, no fixed prefix.
  `${REQUIRED_TAG_LITERAL.kvk} / ${REQUIRED_TAG_LITERAL.vat}`,
  // Adversarial: looks like it is trying to defeat a naive string check.
  "{{supplier_vat_number}".repeat(3) + "}",
  "\n\n\n",
  "a".repeat(5000),
  "unicode: café — 日本語 — {{not_a_real_tag}}",
];

describe("composeLegalIdentityText always embeds both required tags — FR-TPL-009", () => {
  for (const language of ["nl", "en"] as const) {
    for (const freeText of ADVERSARIAL_FREE_TEXT) {
      it(`(${language}) holds both tags for free text ${JSON.stringify(freeText).slice(0, 40)}`, () => {
        const composed = composeLegalIdentityText(language, freeText);
        expect(composed).toContain(REQUIRED_TAG_LITERAL.vat);
        expect(composed).toContain(REQUIRED_TAG_LITERAL.kvk);
      });
    }
  }

  it("never drops the free text itself", () => {
    const composed = composeLegalIdentityText("nl", "IBAN NL00 BANK 0123 4567 89");
    expect(composed).toContain("IBAN NL00 BANK 0123 4567 89");
  });

  it("is exactly the fixed sentence, with nothing appended, when the free text is blank", () => {
    // A blank/whitespace-only field means "nothing else to say", not "a
    // dangling newline after the fixed sentence".
    expect(composeLegalIdentityText("nl", "")).toBe(FIXED_LEGAL_IDENTITY_TEXT.nl);
    expect(composeLegalIdentityText("nl", "   ")).toBe(FIXED_LEGAL_IDENTITY_TEXT.nl);
  });

  it("mirrors api.templates.routes._default_blocks()'s own default string shape", () => {
    expect(FIXED_LEGAL_IDENTITY_TEXT.nl).toBe(
      "KvK {{supplier_kvk_number}} — btw-nr. {{supplier_vat_number}}",
    );
    expect(FIXED_LEGAL_IDENTITY_TEXT.en).toBe(
      "KvK {{supplier_kvk_number}} — VAT no. {{supplier_vat_number}}",
    );
  });
});

describe("decomposeLegalIdentityText — loading a block back into the free-text field", () => {
  it("shows an empty free-text field for the untouched default block", () => {
    expect(decomposeLegalIdentityText("nl", FIXED_LEGAL_IDENTITY_TEXT.nl)).toBe("");
    expect(decomposeLegalIdentityText("en", FIXED_LEGAL_IDENTITY_TEXT.en)).toBe("");
  });

  it("round-trips free text this component saved before", () => {
    for (const freeText of ["IBAN NL00 BANK 0123 4567 89", "line one\nline two", "café"]) {
      const composed = composeLegalIdentityText("nl", freeText);
      expect(decomposeLegalIdentityText("nl", composed)).toBe(freeText);
    }
  });

  it("preserves the whole text, rather than dropping any of it, for a block this component did not produce", () => {
    // e.g. FR-TPL-014's house template push, or a template created directly
    // via the API — some other wording, in some other order, that still
    // happens to carry both tags.
    const houseTemplateText =
      "Onze gegevens: KvK {{supplier_kvk_number}}, btw {{supplier_vat_number}}. Vragen? Bel ons.";

    expect(decomposeLegalIdentityText("nl", houseTemplateText)).toBe(houseTemplateText);
  });

  it("does not crash on text with neither tag", () => {
    expect(() => decomposeLegalIdentityText("nl", "geen placeholders hier")).not.toThrow();
    expect(decomposeLegalIdentityText("nl", "geen placeholders hier")).toBe(
      "geen placeholders hier",
    );
  });

  it("still guarantees both tags on the next save even for text it could not cleanly decompose", () => {
    // The visible cost of the "preserve everything" decision above: composing
    // again prepends the fixed sentence on top of text that may already
    // mention the tags, so they can appear twice. That is an accepted
    // trade-off (see this module's docstring) — what must never happen is
    // the tags going missing, and this proves they do not.
    const houseTemplateText = "Onze gegevens: KvK {{supplier_kvk_number}}. Btw volgt later.";
    const freeText = decomposeLegalIdentityText("nl", houseTemplateText);
    const recomposed = composeLegalIdentityText("nl", freeText);

    expect(recomposed).toContain(REQUIRED_TAG_LITERAL.kvk);
    expect(recomposed).toContain(REQUIRED_TAG_LITERAL.vat);
  });
});
