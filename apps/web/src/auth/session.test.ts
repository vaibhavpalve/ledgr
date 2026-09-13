import { beforeEach, describe, expect, it } from "vitest";

import {
  authHeaders,
  clearSession,
  hasVerifiedStoredSession,
  readStoredSession,
  storeSession,
} from "./session";

beforeEach(() => {
  localStorage.clear();
});

describe("session storage", () => {
  it("round-trips what it stores", () => {
    storeSession({ accessToken: "tok", mfaVerified: true });
    expect(readStoredSession()).toEqual({ accessToken: "tok", mfaVerified: true });
  });

  it("reads null when nothing has been stored", () => {
    expect(readStoredSession()).toBeNull();
  });

  it("reads null for a corrupted value rather than throwing", () => {
    localStorage.setItem("ledgr.session.v1", "{not json");
    expect(readStoredSession()).toBeNull();
  });

  it("reads null for a value missing the fields this format needs", () => {
    localStorage.setItem("ledgr.session.v1", JSON.stringify({ accessToken: "tok" }));
    expect(readStoredSession()).toBeNull();
  });

  it("clearSession removes the stored value", () => {
    storeSession({ accessToken: "tok", mfaVerified: true });
    clearSession();
    expect(readStoredSession()).toBeNull();
  });
});

describe("hasVerifiedStoredSession — Shell's initial `authenticated` guess", () => {
  it("is false with nothing stored", () => {
    expect(hasVerifiedStoredSession()).toBe(false);
  });

  it("is false for a stored session that has not cleared IAM-011's gate", () => {
    // MFA verification does not survive a reload - see session.ts's own
    // docstring on why this answers false rather than true here.
    storeSession({ accessToken: "tok", mfaVerified: false });
    expect(hasVerifiedStoredSession()).toBe(false);
  });

  it("is true for a stored, MFA-verified session", () => {
    storeSession({ accessToken: "tok", mfaVerified: true });
    expect(hasVerifiedStoredSession()).toBe(true);
  });
});

describe("authHeaders", () => {
  it("carries no Authorization header when nothing is stored", () => {
    expect(authHeaders()).toEqual({});
  });

  it("carries the stored token as a Bearer header", () => {
    storeSession({ accessToken: "abc123", mfaVerified: true });
    expect(authHeaders()).toEqual({ Authorization: "Bearer abc123" });
  });
});
