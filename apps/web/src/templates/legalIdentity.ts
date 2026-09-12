/**
 * FR-TPL-009 applied to the one free-text block that carries statutory merge
 * tags — `legal_identity`. A plain shared textarea would make deleting
 * `{{supplier_vat_number}}` or `{{supplier_kvk_number}}` a two-keystroke
 * accident; this module is what makes that structurally impossible instead
 * of merely checked afterwards, the way `TemplateDesigner` makes it
 * impossible to construct a column layout that hides a statutory column.
 *
 * --- The invariant, and how it is kept ---
 *
 * `composeLegalIdentityText` ALWAYS prepends the fixed tag sentence before
 * whatever free text the author typed — unconditionally, not "only if the
 * free text doesn't already have the tags". The requirement (and the test
 * this module ships with) is that the composed text always CONTAINS both
 * tags, not that it contains them exactly once. Prepending unconditionally
 * is what lets the free-text field hold literally anything — empty,
 * adversarial, a string that already mentions `{{supplier_vat_number}}` in
 * some unrelated sentence — without the guarantee depending on parsing that
 * input correctly.
 *
 * --- Loading text this component did not produce ---
 *
 * `decomposeLegalIdentityText` is the inverse, for populating the free-text
 * field when a template is loaded. For a block this component saved before,
 * the existing text starts with exactly the fixed prefix this module still
 * emits, and stripping it back off is exact and lossless.
 *
 * For a block that was NOT produced by this component — created directly via
 * the API, or pushed by a firm's house template (FR-TPL-014) — the existing
 * text will not, in general, start with that literal prefix even if it
 * contains both tags somewhere in its own wording. Rather than guess at
 * which substring is "the fixed part" (a heuristic that would sometimes
 * guess wrong and silently eat someone's prose), this hands back the WHOLE
 * existing text as the free-text portion. Nothing is dropped, and the
 * compose invariant still holds — the fixed prefix is added on top, so the
 * two tags appear at least once even if editing the free text later removes
 * the ones that were already in it. The visible cost is that such a block's
 * composed text can end up mentioning each tag twice (once from the fixed
 * prefix, once from the preserved original wording) until an editor tidies
 * it — a rough edge, not a compliance risk, and the one this module accepts
 * deliberately: see this repo's report on FR-TPL-009 for where this
 * trade-off was decided.
 */

import type { Language } from "@ledgr/i18n";

/** The exact literal `api.templates.compliance.check()` and 0042's CHECK constraint look for. */
export const REQUIRED_TAG_LITERAL = {
  vat: "{{supplier_vat_number}}",
  kvk: "{{supplier_kvk_number}}",
} as const;

/**
 * Mirrors `api.templates.routes._default_blocks()`'s own default string
 * shape exactly, so a template nobody has touched yet round-trips through
 * compose/decompose as an EMPTY free-text field, rather than as free text
 * that happens to contain the whole default sentence.
 */
export const FIXED_LEGAL_IDENTITY_TEXT: Record<Language, string> = {
  nl: `KvK ${REQUIRED_TAG_LITERAL.kvk} — btw-nr. ${REQUIRED_TAG_LITERAL.vat}`,
  en: `KvK ${REQUIRED_TAG_LITERAL.kvk} — VAT no. ${REQUIRED_TAG_LITERAL.vat}`,
};

/** What gets SENT to the API for one language of the `legal_identity` block. */
export function composeLegalIdentityText(language: Language, freeText: string): string {
  const fixed = FIXED_LEGAL_IDENTITY_TEXT[language];
  return freeText.trim().length > 0 ? `${fixed}\n${freeText}` : fixed;
}

/** What to show in the free-text field when a template is loaded — see this module's docstring. */
export function decomposeLegalIdentityText(language: Language, existingText: string): string {
  const fixed = FIXED_LEGAL_IDENTITY_TEXT[language];
  if (existingText === fixed) return "";
  const prefix = `${fixed}\n`;
  if (existingText.startsWith(prefix)) return existingText.slice(prefix.length);
  return existingText;
}
