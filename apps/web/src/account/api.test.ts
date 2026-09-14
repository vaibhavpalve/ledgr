import { describe, expect, it } from "vitest";

import { fakeFetch, jsonResponse, problemResponse } from "../testing/fakeFetch";
import { AccountApi, ApiError, OfflineError } from "./api";

const me = {
  user: { id: "u1", email: "a@b.nl", language: "nl", email_verified: false },
  organization: { id: "o1", name: "Van Doorn Bouw B.V.", kind: "business", kvk_number: "34281907" },
  administrations: [],
  active_administration_id: null,
  mfa: { has_totp: true, has_passkey: false },
  onboarding: { needs_administration: true },
};

describe("AccountApi", () => {
  it("GET /v1/me is a plain read with the language header and no idempotency key", async () => {
    const { impl, calls } = fakeFetch({ "GET /v1/me": () => jsonResponse(me) });
    const api = new AccountApi({ language: () => "en", fetchImpl: impl });

    const result = await api.getMe();

    expect(result.onboarding.needs_administration).toBe(true);
    expect(calls[0]?.headers["accept-language"]).toBe("en");
    expect(calls[0]?.headers["idempotency-key"]).toBeUndefined();
  });

  it("lists sessions from either a bare array or a wrapped one, and marks the current session", async () => {
    const rows = [
      { id: "s1", created_at: "2026-09-14T08:00:00Z", last_active_at: "2026-09-14T09:00:00Z", expires_at: "2026-09-15T08:00:00Z", is_current: true },
      { id: "s2", created_at: "2026-09-10T08:00:00Z", last_seen_at: "2026-09-11T09:00:00Z", expires_at: "2026-09-11T08:00:00Z" },
    ];
    const bare = new AccountApi({
      language: () => "nl",
      fetchImpl: fakeFetch({ "GET /v1/me/sessions": () => jsonResponse(rows) }).impl,
    });
    const wrapped = new AccountApi({
      language: () => "nl",
      fetchImpl: fakeFetch({ "GET /v1/me/sessions": () => jsonResponse({ sessions: rows }) }).impl,
    });

    const [fromBare, fromWrapped] = await Promise.all([bare.listSessions(), wrapped.listSessions()]);

    expect(fromBare).toEqual(fromWrapped);
    expect(fromBare[0]?.is_current).toBe(true);
    expect(fromBare[1]?.is_current).toBe(false);
    expect(fromBare[1]?.last_active_at).toBe("2026-09-11T09:00:00Z");
  });

  it("DELETE carries a fresh idempotency key and accepts an empty 204", async () => {
    const { impl, calls } = fakeFetch({
      "DELETE /v1/me/sessions/s2": () => new Response(null, { status: 204 }),
    });
    const api = new AccountApi({ language: () => "nl", fetchImpl: impl });

    await expect(api.revokeSession("s2")).resolves.toBeUndefined();
    expect(calls[0]?.headers["idempotency-key"]).toMatch(/[0-9a-f-]{36}/);
  });

  it("changePassword sends the §4.4 body", async () => {
    const { impl, calls } = fakeFetch({
      "POST /v1/me/password": () => jsonResponse({ status: "changed" }),
    });
    const api = new AccountApi({ language: () => "nl", fetchImpl: impl });

    await api.changePassword("old-one", "new-and-long-enough");

    expect(calls[0]?.body).toEqual({ current_password: "old-one", new_password: "new-and-long-enough" });
  });

  it("surfaces the server's reason and sentence as an ApiError", async () => {
    const { impl } = fakeFetch({
      "DELETE /v1/me/passkeys/p1": () =>
        problemResponse(409, "last_factor", "Dit is uw enige tweede factor."),
    });
    const api = new AccountApi({ language: () => "nl", fetchImpl: impl });

    const error = await api.removePasskey("p1").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).reason).toBe("last_factor");
    expect((error as ApiError).message).toBe("Dit is uw enige tweede factor.");
  });

  it("a thrown fetch is an OfflineError, not an ApiError", async () => {
    const api = new AccountApi({
      language: () => "nl",
      fetchImpl: (async () => {
        throw new TypeError("Failed to fetch");
      }) as unknown as typeof fetch,
    });

    await expect(api.getMe()).rejects.toBeInstanceOf(OfflineError);
  });
});
