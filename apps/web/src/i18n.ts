/**
 * Wiring the shared i18n runtime to this app's browser and API — IAM-010g,
 * FR-LOC-001a, FR-LOC-001b.
 *
 * `@ledgr/i18n` is deliberately free of `navigator`, `location`, `fetch` and
 * `localStorage`: it is shared with React Native (MOB-016), and its resolution
 * rule is worth testing without a DOM. This file is where those come in, and
 * it is the only place in the web app that knows how a language preference
 * reaches the server.
 */

import { readStoredLanguage, resolveLanguage, storeLanguage, type Language } from "@ledgr/i18n";

/**
 * The language to render the very first frame in.
 *
 * Called before React mounts, so the login screen is already in the right
 * language rather than flashing Dutch and correcting itself — IAM-010g puts
 * the language control on the pre-login screens, and a control that appears
 * to change the answer after the fact is worse than none.
 *
 * The signed-in user's own setting (`accountLanguage`) is not available yet
 * at this point: it arrives with `GET /v1/me/language` after authentication,
 * and is applied then. See `applyAccountLanguage` below.
 */
export function initialLanguage(): Language {
  return resolveLanguage({
    storedChoice: readStoredLanguage(),
    preferredLanguages: typeof navigator === "undefined" ? [] : navigator.languages,
    hostname: typeof location === "undefined" ? null : location.hostname,
  });
}

/**
 * What to switch to once the account's own setting is known.
 *
 * Returns the language to display and whether the account needs seeding —
 * IAM-010g's "the choice persists on the device and is applied to the account
 * after first login". `null` from the API means this person has never chosen,
 * which is why `GET /v1/me/language` returns null rather than a default: the
 * two cases need opposite actions, and a default would make them identical.
 */
export function reconcileWithAccount(
  accountLanguage: string | null,
  deviceLanguage: Language,
): { language: Language; seedAccount: boolean } {
  const resolved = resolveLanguage({ accountLanguage, storedChoice: deviceLanguage });
  return { language: resolved, seedAccount: accountLanguage === null };
}

/**
 * IAM-010g's second half: "applied to the account after first login."
 *
 * Run once authentication lands. Reads the account's own setting, decides
 * which language wins, and seeds the account from the device when this person
 * has never chosen one.
 *
 * The seeding write is what makes the pre-login choice mean anything beyond
 * this browser. Somebody who picked English on the login screen of a machine
 * they will never use again should still find LEDGR in English on their own
 * one, and this is the only moment that can be arranged — after that, the
 * account is authoritative (FR-LOC-001b) and the device is just a cache.
 *
 * Returns the language to display. On failure it returns the device language
 * unchanged rather than throwing: an unreadable preference is a reason to
 * keep showing what the person already chose, never a reason to fail a login
 * that otherwise worked.
 */
export async function applyAccountLanguage(
  deviceLanguage: Language,
  fetchImpl: typeof fetch = fetch,
): Promise<Language> {
  let accountLanguage: string | null = null;
  try {
    const response = await fetchImpl("/v1/me/language", {
      headers: languageHeaders(deviceLanguage),
    });
    if (!response.ok) return deviceLanguage;
    // `null` here is "never chosen", which is why GET returns null rather
    // than a default — see the endpoint's docstring. A default would make
    // "never chosen" and "chose Dutch" identical, and this function would
    // then either never seed or always overwrite.
    accountLanguage = ((await response.json()) as { language: string | null }).language;
  } catch {
    return deviceLanguage;
  }

  const { language, seedAccount } = reconcileWithAccount(accountLanguage, deviceLanguage);

  if (seedAccount) {
    // The account has never been told. This is the write IAM-010g is about.
    await persistLanguage(language, fetchImpl);
  } else {
    // The account already said this, so only the device needs to learn it —
    // caching it here is what makes the next reload start in the right
    // language before the account has been read. Writing it back through
    // `persistLanguage` would PUT a value straight to the place it was just
    // read from: a pointless round trip on every sign-in from a machine that
    // remembered something else.
    storeLanguage(language);
  }

  return language;
}

/** Headers every API call carries, not just the language one. */
export function languageHeaders(language: Language): Record<string, string> {
  // FR-UX-007: this is how an error comes back in the language the person is
  // actually looking at. The API answers from this header rather than from
  // the stored column, so a language switched a moment ago is already in
  // effect on the server — see api.i18n.language's module docstring.
  //
  // Set explicitly rather than left to the browser: `Accept-Language` would
  // otherwise carry the browser's preference, which is precisely what the
  // in-app control exists to override.
  return { "Accept-Language": language };
}

/**
 * Persist a language choice — device first, then account.
 *
 * Device first, and not merely for ordering: the device write is synchronous
 * and cannot fail in a way that matters, so a reload lands in the language
 * the person chose even if the network did not cooperate. The account write
 * is what makes the choice follow them to another machine (FR-LOC-001b).
 *
 * Deliberately does not throw. This runs AFTER the UI has already switched
 * (see I18nProvider's `onLanguageChange`), so there is nothing to roll back
 * and rolling back would be wrong anyway: the person asked for English, they
 * are looking at English, and undoing that because a write failed is a worse
 * outcome than a preference that does not outlive the session.
 */
export async function persistLanguage(
  language: Language,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  storeLanguage(language);

  try {
    await fetchImpl("/v1/me/language", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        ...languageHeaders(language),
        // NFR-032: every mutating endpoint requires one, and this is a
        // mutating endpoint like any other. A fresh key per attempt, because
        // each click is a new intention rather than a retry of the last.
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({ language }),
    });
  } catch {
    // Swallowed on purpose — see the docstring. A signed-out visitor on the
    // login screen reaches here too, and their 401 is not something to
    // report: the device already remembers, and first login applies it.
  }
}
