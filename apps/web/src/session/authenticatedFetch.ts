/**
 * The `fetch` every authenticated API client is built on — the seam
 * `auth/session.ts` named as "real, separate follow-up work": wiring the
 * stored bearer token into `capture/api.ts`, `invoicing/api.ts` and the rest
 * without teaching each of them where the token lives.
 *
 * Two jobs, and only two:
 *
 *   1. `Authorization: Bearer …` from the stored session, unless the caller
 *      already set one (`auth/api.ts` sets its own for the MFA surface).
 *   2. A 401 means the session is over — expired, or revoked elsewhere, which
 *      the session-backed tenant context (§4.1) now refuses on the very next
 *      request. `onUnauthorized` is how the app learns that from ANY call,
 *      so the person lands on the sign-in screen with a sentence rather than
 *      on a screen full of broken panels.
 *
 * The response is still returned to the caller after `onUnauthorized` fires:
 * the client's own error path runs as normal (its screen shows its own
 * error), and by then the redirect to `/login` has already been queued. The
 * screen being replaced is what makes that harmless.
 *
 * `base` is resolved at call time, not captured, so a test that stubs
 * `globalThis.fetch` after this was built still gets the stub — the same
 * reason `api/http.ts` reads `fetchImpl` late.
 */

import { authHeaders } from "../auth/session";

export function createAuthenticatedFetch({
  base,
  onUnauthorized,
}: {
  base?: () => typeof fetch;
  onUnauthorized: () => void;
}): typeof fetch {
  const resolveBase = base ?? (() => globalThis.fetch);

  return async (input, init) => {
    const headers = new Headers(init?.headers);
    if (!headers.has("Authorization")) {
      for (const [name, value] of Object.entries(authHeaders())) headers.set(name, value);
    }
    const response = await resolveBase()(input, { ...init, headers });
    if (response.status === 401) onUnauthorized();
    return response;
  };
}
