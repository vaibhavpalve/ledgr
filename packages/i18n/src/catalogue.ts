/**
 * Reading the message catalogue — FR-LOC-001, FR-LOC-001c, FR-UX-007.
 *
 * The catalogue itself is ../catalogue/*.json and its contract is documented
 * there. This module is the runtime side of it: types, the merged lookup, and
 * interpolation.
 *
 * --- There is no fallback between languages, on purpose ---
 *
 * Nearly every i18n library resolves a missing Dutch string by showing the
 * English one. FR-LOC-001 forbids exactly that — "A missing translation is a
 * release blocker, not a fallback to English" — and the reason is not
 * tidiness. A fallback makes a missing translation invisible in every
 * environment where somebody might notice it, so the Dutch product ships with
 * English sentences scattered through it and nobody has a list of them.
 *
 * So `translate` THROWS on a key it cannot resolve, and the safety net is
 * scripts/check_translations.py (FR-LOC-001d) failing the build first. The
 * order matters: the check is what makes throwing at runtime a safe design
 * rather than a reckless one.
 */

import type { Language } from "./language";
import { pluralCategory } from "./language";

import authFile from "../catalogue/auth.json";
import captureFile from "../catalogue/capture.json";
import clientFile from "../catalogue/client.json";
import commonFile from "../catalogue/common.json";
import customersFile from "../catalogue/customers.json";
import errorsFile from "../catalogue/errors.json";
import invoiceFile from "../catalogue/invoice.json";
import glossaryFile from "../catalogue/glossary.json";
import ledgerFile from "../catalogue/ledger.json";
import mobileFile from "../catalogue/mobile.json";
import onboardingFile from "../catalogue/onboarding.json";
import rolesFile from "../catalogue/roles.json";
import settingsFile from "../catalogue/settings.json";

/** A single form, or the two CLDR categories Dutch and English share. */
export type MessageText = string | { readonly one: string; readonly other: string };

export interface MessageRecord {
  /** Context for whoever writes or reviews the translation. Never rendered. */
  readonly note?: string;
  /** Required when the two languages match — see ../catalogue/README.md. */
  readonly identical?: boolean;
  readonly nl: MessageText;
  readonly en: MessageText;
}

export interface GlossaryTerm {
  /** The term as it is written in Dutch, and in English when kept. */
  readonly term: string;
  /** FR-LOC-001c: this term survives translation unchanged. */
  readonly keepDutch: boolean;
  /** The hover or tap definition the requirement asks for, in both languages. */
  readonly definition: MessageRecord;
}

interface CatalogueFile {
  readonly namespace: string;
  readonly messages: Readonly<Record<string, MessageRecord>>;
}

interface GlossaryFile {
  readonly namespace: string;
  readonly terms: Readonly<Record<string, GlossaryTerm>>;
}

// The JSON files are data, and TypeScript infers a wildly specific literal
// type for each. Widening once here, at the boundary, keeps the assertion in
// one place — and the shapes are checked for real by
// scripts/check_translations.py, which reads the same files without believing
// anything TypeScript says about them.
const FILES: readonly CatalogueFile[] = [
  commonFile as CatalogueFile,
  authFile as CatalogueFile,
  captureFile as CatalogueFile,
  clientFile as CatalogueFile,
  errorsFile as CatalogueFile,
  invoiceFile as CatalogueFile,
  mobileFile as CatalogueFile,
  rolesFile as CatalogueFile,
  // The routed web app's four namespaces (docs/founder-review-2026-09-14.md
  // §5.2). Listed here AND in api.i18n.catalogue.CATALOGUE_FILES;
  // scripts/check_translations.py fails if the two lists differ.
  onboardingFile as CatalogueFile,
  settingsFile as CatalogueFile,
  ledgerFile as CatalogueFile,
  customersFile as CatalogueFile,
];

function mergeMessages(files: readonly CatalogueFile[]): Record<string, MessageRecord> {
  const merged: Record<string, MessageRecord> = {};
  for (const file of files) {
    for (const [key, record] of Object.entries(file.messages)) {
      if (key in merged) {
        // Two files claiming one key means one of them silently loses. The
        // namespace rule (every key in client.json starts with `client.`)
        // makes this unreachable, and it is asserted rather than assumed
        // because the failure would be a message that changes depending on
        // file load order.
        throw new Error(`duplicate message key across catalogue files: ${key}`);
      }
      merged[key] = record;
    }
  }
  return merged;
}

export const MESSAGES: Readonly<Record<string, MessageRecord>> = mergeMessages(FILES);

export const GLOSSARY: Readonly<Record<string, GlossaryTerm>> = (glossaryFile as GlossaryFile)
  .terms;

export type MessageKey = string;

export type MessageParams = Readonly<Record<string, string | number>>;

export class MissingMessageError extends Error {}

/** Whether a key exists, for the rare caller that builds one at runtime. */
export function hasMessage(key: MessageKey): boolean {
  return key in MESSAGES;
}

/**
 * The message for `key`, in `language`, with `{placeholders}` filled in.
 *
 * Throws when the key is unknown, when a counted message is given no `count`,
 * or when a placeholder has no value. All three are bugs that would otherwise
 * reach a user as a raw key, the wrong plural form, or a literal `{query}` on
 * screen — and FR-UX-007 is explicit that developer-facing strings never
 * reach a user.
 */
export function translate(key: MessageKey, language: Language, params: MessageParams = {}): string {
  const record = MESSAGES[key];
  if (record === undefined) {
    throw new MissingMessageError(
      `no message ${JSON.stringify(key)} in the catalogue. Add it to ` +
        `packages/i18n/catalogue/ in BOTH languages (FR-LOC-001).`,
    );
  }

  const text = language === "nl" ? record.nl : record.en;
  return interpolate(select(text, key, language, params), key, params);
}

function select(
  text: MessageText,
  key: MessageKey,
  language: Language,
  params: MessageParams,
): string {
  if (typeof text === "string") return text;

  const count = params.count;
  if (typeof count !== "number") {
    throw new MissingMessageError(
      `${key} is a counted message and needs a numeric \`count\` parameter; ` +
        `got ${JSON.stringify(count)}.`,
    );
  }
  return pluralCategory(language, count) === "one" ? text.one : text.other;
}

const PLACEHOLDER = /\{(\w+)\}/g;

function interpolate(text: string, key: MessageKey, params: MessageParams): string {
  return text.replace(PLACEHOLDER, (_match, name: string) => {
    const value = params[name];
    if (value === undefined) {
      throw new MissingMessageError(
        `${key} expects a {${name}} parameter and was given none. The catalogue ` +
          `keeps the same placeholders in both languages, so this is missing in ` +
          `both.`,
      );
    }
    return String(value);
  });
}

/**
 * FR-LOC-001c's hover or tap definition. Returns `undefined` for an unknown
 * term rather than throwing: a glossary lookup is decoration around a term
 * that is already legible on its own, so a missing entry should cost the
 * tooltip and nothing else.
 */
export function glossaryDefinition(termId: string, language: Language): string | undefined {
  const entry = GLOSSARY[termId];
  if (entry === undefined) return undefined;
  const text = language === "nl" ? entry.definition.nl : entry.definition.en;
  return typeof text === "string" ? text : text.other;
}

export function glossaryTerm(termId: string): GlossaryTerm | undefined {
  return GLOSSARY[termId];
}
