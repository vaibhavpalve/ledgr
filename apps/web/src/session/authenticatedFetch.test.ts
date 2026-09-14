import { afterEach, describe, expect, it, vi } from "vitest";

import { clearSession, storeSession } from "../auth/session";
import { createAuthenticatedFetch } from "./authenticatedFetch";

afterEach(() => {
  clearSession();
});

function recordingFetch(status: number) {
  const calls: Array<{ url: string; headers: Headers }> = [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), headers: new Headers(init?.headers) });
    return new Response(null, { status });
  }) as unknown as typeof fetch;
  return { impl, calls };
}

describe("createAuthenticatedFetch", () => {
  it("adds the stored bearer token to every call", async () => {
    storeSession({ accessToken: "tok-123", mfaVerified: true });
    const { impl, calls } = recordingFetch(200);
    const onUnauthorized = vi.fn();
    const fetchImpl = createAuthenticatedFetch({ base: () => impl, onUnauthorized });

    await fetchImpl("/v1/me", { headers: { "Accept-Language": "nl" } });

    expect(calls[0]?.headers.get("Authorization")).toBe("Bearer tok-123");
    // The caller's own headers survive the merge.
    expect(calls[0]?.headers.get("Accept-Language")).toBe("nl");
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("leaves an Authorization header the caller set alone", async () => {
    storeSession({ accessToken: "stored", mfaVerified: true });
    const { impl, calls } = recordingFetch(200);
    const fetchImpl = createAuthenticatedFetch({ base: () => impl, onUnauthorized: vi.fn() });

    await fetchImpl("/v1/auth/mfa/totp/verify", { headers: { Authorization: "Bearer explicit" } });

    expect(calls[0]?.headers.get("Authorization")).toBe("Bearer explicit");
  });

  it("sends no Authorization header when nothing is stored", async () => {
    const { impl, calls } = recordingFetch(200);
    const fetchImpl = createAuthenticatedFetch({ base: () => impl, onUnauthorized: vi.fn() });

    await fetchImpl("/v1/me");

    expect(calls[0]?.headers.has("Authorization")).toBe(false);
  });

  it("reports a 401 and still hands the response back to the caller", async () => {
    storeSession({ accessToken: "stale", mfaVerified: true });
    const { impl } = recordingFetch(401);
    const onUnauthorized = vi.fn();
    const fetchImpl = createAuthenticatedFetch({ base: () => impl, onUnauthorized });

    const response = await fetchImpl("/v1/administrations/a/dashboard");

    expect(response.status).toBe(401);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not treat a 403 as the session ending", async () => {
    const { impl } = recordingFetch(403);
    const onUnauthorized = vi.fn();
    const fetchImpl = createAuthenticatedFetch({ base: () => impl, onUnauthorized });

    await fetchImpl("/v1/administrations/a/customers");

    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});
