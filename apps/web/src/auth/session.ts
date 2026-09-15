/**
 * Where the bearer token from ADR-054's signup/login/MFA endpoints lives in
 * this browser — the counterpart to `../i18n.ts`'s "the only place in the
 * web app that knows how a language preference reaches the server."
 *
 * `localStorage`, mirroring `@ledgr/i18n`'s `LANGUAGE_STORAGE_KEY` pattern:
 * survives a reload (so a person is not signed out by refreshing the page)
 * and is per-device, never sent anywhere except as the `Authorization`
 * header this module also builds.
 *
 * --- What this deliberately does not do ---
 *
 * It does not attach `Authorization` to every API client in this app —
 * `capture/api.ts`, `client/api.ts` and the others were all built and tested
 * before ADR-054 existed, and none of them read this. Wiring the token into
 * every existing feature client is real, separate follow-up work (the same
 * "named gap, not silently missing" discipline ADR-054 itself uses for its
 * own two deferred pieces), not something to fold silently into shipping
 * the auth screens. `auth/api.ts` is the one client that reads it today,
 * because it is the only one whose endpoints need it.
 */

const STORAGE_KEY = "ledgr.session.v1";
// ADR-061: deliberately a SEPARATE key from STORAGE_KEY, and never cleared
// by clearSession()/signing out - the whole point of "remember this device"
// is that it survives the session it was issued in. It expires (or is
// revoked from the security settings screen) on the server; this browser
// just holds onto whatever it was last given.
const TRUSTED_DEVICE_STORAGE_KEY = "ledgr.trusted_device.v1";

export interface StoredSession {
  accessToken: string;
  mfaVerified: boolean;
}

export function readStoredSession(): StoredSession | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredSession>;
    if (typeof parsed.accessToken !== "string" || typeof parsed.mfaVerified !== "boolean") {
      return null;
    }
    return { accessToken: parsed.accessToken, mfaVerified: parsed.mfaVerified };
  } catch {
    // A corrupted or pre-format value is the same as no session — never a
    // reason to throw during render.
    return null;
  }
}

export function storeSession(session: StoredSession): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  } catch {
    // Storage can be unavailable (private browsing, quota) - the session
    // still works for the rest of this page's lifetime via in-memory state;
    // it just will not survive a reload. Not a reason to fail sign-in.
  }
}

export function clearSession(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // See storeSession.
  }
}

/**
 * `Shell`'s initial "am I signed in" guess, before any explicit
 * `authenticated` prop overrides it (tests pass one directly; production
 * never does — see `App.tsx`). A stored session with `mfaVerified: false`
 * answers `false` here on purpose: MFA verification does not survive a
 * reload today (there is no "resume where you left off" step-up flow), so
 * a person who left mid-enrolment starts over rather than landing in a
 * state this app cannot recover into.
 */
export function hasVerifiedStoredSession(): boolean {
  const session = readStoredSession();
  return session !== null && session.mfaVerified;
}

export function authHeaders(): Record<string, string> {
  const session = readStoredSession();
  return session === null ? {} : { Authorization: `Bearer ${session.accessToken}` };
}

/** What `login()` sends alongside the password, so a device this browser
 * was already remembered on (ADR-061) can start pre-verified. `null` when
 * nothing was ever stored, or storage is unavailable - login() treats an
 * absent token exactly like an invalid one, so there is no special case
 * here for "never enrolled" versus "storage blocked".
 */
export function readTrustedDeviceToken(): string | null {
  try {
    return localStorage.getItem(TRUSTED_DEVICE_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Called when an MFA verification response carries a fresh
 * `trusted_device_token` (only when the person checked "remember this
 * device"). Overwrites whatever was stored before - one device only ever
 * needs to remember its OWN most recent grant.
 */
export function storeTrustedDeviceToken(token: string): void {
  try {
    localStorage.setItem(TRUSTED_DEVICE_STORAGE_KEY, token);
  } catch {
    // See storeSession - a device simply will not be remembered next time.
  }
}

/** Only called from the security settings screen, when a person revokes
 * this device from their own trusted-device list - not from
 * clearSession()/sign-out, which must leave this alone.
 */
export function clearTrustedDeviceToken(): void {
  try {
    localStorage.removeItem(TRUSTED_DEVICE_STORAGE_KEY);
  } catch {
    // See storeSession.
  }
}
