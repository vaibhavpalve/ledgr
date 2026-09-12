/**
 * The device's remembered language choice — IAM-010g.
 *
 *   "The choice persists on the device and is applied to the account after
 *    first login."
 *
 * Device storage, not the account, because this has to work with no account:
 * the language control is on the login and signup screens, before anything
 * has authenticated. Once there is an account, `users.language` is
 * authoritative (FR-LOC-001b, and see resolveLanguage in language.ts) and
 * this becomes the seed for it and the memory for the next signed-out visit.
 *
 * Every access is wrapped, because `localStorage` does not merely return
 * nothing when it is unavailable — it THROWS on access in a browser
 * configured to block site data, and in some private-window and embedded
 * contexts. An unhandled throw here would take down the login screen over a
 * cosmetic preference, so the failure mode is "we do not remember your
 * choice", never "you cannot sign in".
 */

import type { Language } from "./language";
import { isLanguage } from "./language";

export const LANGUAGE_STORAGE_KEY = "ledgr.language";

interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function defaultStore(): KeyValueStore | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function readStoredLanguage(store: KeyValueStore | null = defaultStore()): Language | null {
  try {
    const stored = store?.getItem(LANGUAGE_STORAGE_KEY) ?? null;
    return isLanguage(stored) ? stored : null;
  } catch {
    return null;
  }
}

/**
 * Returns whether the choice was actually persisted, so a caller that wants
 * to say "we could not remember this on this device" can. Nothing in the app
 * does today, and returning it costs nothing and keeps the failure knowable
 * rather than swallowed.
 */
export function storeLanguage(
  language: Language,
  store: KeyValueStore | null = defaultStore(),
): boolean {
  try {
    store?.setItem(LANGUAGE_STORAGE_KEY, language);
    return store !== null;
  } catch {
    return false;
  }
}
