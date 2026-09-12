/**
 * Rendering a role name — FR-LOC-001, and the one place in this package where
 * "translate it" is a decision rather than a given.
 *
 * --- Two kinds of role, and only one of them is ours ---
 *
 *   SYSTEM roles   the twelve of PRD §8.4. Their names are LEDGR's own
 *                  vocabulary, identical in every tenant, and they are
 *                  user-facing text like any other — so they are translated.
 *   CUSTOM roles   composed by an organization (ADR-013, migration 0011).
 *                  The name is chosen by that organization and is TENANT
 *                  DATA. It is rendered verbatim, for exactly the reason a
 *                  client's legal name is: it is not ours to reword, and two
 *                  colleagues discussing "who has Betaalfiat?" must both be
 *                  looking at that word.
 *
 * The API says which is which (`role_is_system` on a switcher entry), because
 * only the database knows. Guessing from whether the name happens to be in
 * the catalogue would be wrong in the case that matters most: an organization
 * is free to name a custom role "Bookkeeper", and translating that to
 * "Boekhouder" would show a Dutch reader a role their organization does not
 * have.
 *
 * --- Why the identifier is never translated ---
 *
 * Only the LABEL is. `role.name` remains the identifier everywhere else —
 * authorization matches on it (api.authz.matrix, migration 0010), audit
 * entries record it, grants name it. A translated identifier would mean a
 * grant that reads differently depending on who fetched it, which is not a
 * localisation feature but a data corruption.
 */

import { hasMessage, translate } from "./catalogue";
import type { Language } from "./language";

/**
 * `"Organization Admin"` → `"roles.organization_admin"`.
 *
 * Derived rather than kept as a second mapping table, so adding a role to
 * PRD §8.4 means adding one catalogue record and nothing else.
 * apps/api/tests/i18n/test_role_names.py walks the real role catalogue and
 * fails the build if any system role's key is missing here.
 */
export function roleMessageKey(roleName: string): string {
  const slug = roleName
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `roles.${slug}`;
}

export interface RoleLabelOptions {
  /** From the API. False for a custom role, whose name is tenant data. */
  isSystem: boolean;
}

/**
 * What to show a person where a role name goes.
 *
 * Falls back to the raw name for a system role with no catalogue entry, and
 * that is the one fallback in this package — worth being explicit about,
 * because FR-LOC-001 rules out falling back from Dutch to English and this is
 * deliberately not that.
 *
 * Falling back BETWEEN LANGUAGES hides a missing translation from everyone who
 * might have noticed it. This falls back from a translated label to the
 * identifier, which is conspicuous rather than hidden, and it exists because a
 * role row can appear in the database without a deploy — a migration, a
 * support action, an `is_system` flag set on a row this build has never seen.
 * The alternative is `translate` throwing, which would take the client
 * switcher down over a data condition. Completeness is enforced where it can
 * be enforced: in CI, against the actual role catalogue.
 */
export function roleLabel(
  roleName: string,
  language: Language,
  { isSystem }: RoleLabelOptions,
): string {
  if (!isSystem) return roleName;
  const key = roleMessageKey(roleName);
  return hasMessage(key) ? translate(key, language) : roleName;
}
