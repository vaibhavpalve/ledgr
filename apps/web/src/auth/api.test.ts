import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, AuthApi } from "./api";
import { storeSession } from "./session";

function respond(status: number, body?: unknown): typeof fetch {
  return vi.fn(async () =>
    body === undefined
      ? new Response(null, { status })
      : new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
  ) as unknown as typeof fetch;
}

function api(fetchImpl: typeof fetch): AuthApi {
  return new AuthApi({ language: () => "nl", fetchImpl });
}

function headersOf(fetchImpl: ReturnType<typeof vi.fn>): Record<string, string> {
  const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
  return init.headers as Record<string, string>;
}

beforeEach(() => {
  localStorage.clear();
});

describe("AuthApi.signup — ADR-054/FR-MDL-001", () => {
  it("posts snake_case fields and maps the response to AuthResult", async () => {
    const fetchImpl = respond(200, {
      access_token: "tok",
      token_type: "bearer",
      mfa_verified: false,
      mfa: { has_passkey: false, has_totp: false },
    });

    const result = await api(fetchImpl).signup({
      accountModel: "firm",
      organizationName: "Bakker Consultancy",
      kvkNumber: "12345678",
      email: "owner@example.com",
      password: "correct horse battery staple",
    });

    expect(result).toEqual({
      accessToken: "tok",
      mfaVerified: false,
      enrollment: { hasPasskey: false, hasTotp: false },
    });
    const [url, init] = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/v1/auth/signup");
    expect(JSON.parse(String(init.body))).toEqual({
      account_model: "firm",
      organization_name: "Bakker Consultancy",
      kvk_number: "12345678",
      email: "owner@example.com",
      password: "correct horse battery staple",
    });
  });

  it("carries no Authorization header - there is no session yet", async () => {
    const fetchImpl = respond(200, {
      access_token: "tok",
      token_type: "bearer",
      mfa_verified: true,
    });
    await api(fetchImpl).signup({
      accountModel: "self_managed",
      organizationName: "Bakker",
      kvkNumber: null,
      email: "a@example.com",
      password: "x",
    });
    expect(
      headersOf(fetchImpl as unknown as ReturnType<typeof vi.fn>).Authorization,
    ).toBeUndefined();
  });

  it("throws ApiError with the server's reason on a refusal", async () => {
    const fetchImpl = respond(409, {
      detail: {
        reason: "email_already_registered",
        message: "Dit e-mailadres is al geregistreerd.",
      },
    });

    const failure = api(fetchImpl).signup({
      accountModel: "self_managed",
      organizationName: "Bakker",
      kvkNumber: null,
      email: "a@example.com",
      password: "x",
    });

    await expect(failure).rejects.toThrow(ApiError);
    await failure.catch((error: ApiError) => {
      expect(error.status).toBe(409);
      expect(error.reason).toBe("email_already_registered");
      expect(error.message).toBe("Dit e-mailadres is al geregistreerd.");
    });
  });
});

describe("AuthApi.login", () => {
  it("mfaVerified true carries no enrollment status - see _auth_response", async () => {
    const fetchImpl = respond(200, {
      access_token: "tok",
      token_type: "bearer",
      mfa_verified: true,
    });

    const result = await api(fetchImpl).login("a@example.com", "x");
    expect(result).toEqual({ accessToken: "tok", mfaVerified: true, enrollment: null });
  });
});

describe("AuthApi's authenticated calls — the MFA enrolment/verification surface", () => {
  it("attaches Authorization from whatever session.ts currently has stored", async () => {
    storeSession({ accessToken: "stored-token", mfaVerified: false });
    const fetchImpl = respond(200, { secret: "ABC", provisioning_uri: "otpauth://..." });

    await api(fetchImpl).mfaTotpEnrollBegin();

    expect(headersOf(fetchImpl as unknown as ReturnType<typeof vi.fn>).Authorization).toBe(
      "Bearer stored-token",
    );
  });

  it("still sends a mutating call with no Authorization when nothing is stored, rather than crashing", async () => {
    const fetchImpl = respond(200, { secret: "ABC", provisioning_uri: "otpauth://..." });
    await api(fetchImpl).mfaTotpEnrollBegin();
    expect(
      headersOf(fetchImpl as unknown as ReturnType<typeof vi.fn>).Authorization,
    ).toBeUndefined();
  });

  it("logout carries a fresh Idempotency-Key (NFR-032)", async () => {
    storeSession({ accessToken: "tok", mfaVerified: true });
    const fetchImpl = respond(200, { status: "logged_out" });

    await api(fetchImpl).logout();

    const [, init] = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
  });

  it("mfaTotpEnrollConfirm maps camelCase back to the wire shape and the response back", async () => {
    storeSession({ accessToken: "tok", mfaVerified: false });
    const fetchImpl = respond(200, {
      access_token: "tok2",
      token_type: "bearer",
      mfa_verified: true,
    });

    const result = await api(fetchImpl).mfaTotpEnrollConfirm("SECRET", "123456");

    expect(result).toEqual({ accessToken: "tok2", mfaVerified: true, enrollment: null });
    const [, init] = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(JSON.parse(String(init.body))).toEqual({ secret: "SECRET", code: "123456" });
  });
});

describe("AuthApi.loginGoogleCallback — IAM-010c's link-required branch", () => {
  it("resolves signed_in for the ordinary auth response shape", async () => {
    const fetchImpl = respond(200, {
      access_token: "tok",
      token_type: "bearer",
      mfa_verified: true,
    });

    const outcome = await api(fetchImpl).loginGoogleCallback("code", "state");

    expect(outcome).toEqual({
      kind: "signed_in",
      result: { accessToken: "tok", mfaVerified: true, enrollment: null },
    });
  });

  it("resolves link_required for api.auth.google_signin's GoogleSignInLinkRequired body", async () => {
    const fetchImpl = respond(200, {
      status: "link_required",
      message: "Dit Google-account is nog niet gekoppeld.",
    });

    const outcome = await api(fetchImpl).loginGoogleCallback("code", "state");

    expect(outcome).toEqual({
      kind: "link_required",
      message: "Dit Google-account is nog niet gekoppeld.",
    });
  });

  it("resolves signup_required for a brand-new Google identity (FR-MDL-001)", async () => {
    const fetchImpl = respond(200, {
      status: "signup_required",
      ticket: "ticket-abc",
      email: "brand.new@example.com",
    });

    const outcome = await api(fetchImpl).loginGoogleCallback("code", "state");

    expect(outcome).toEqual({
      kind: "signup_required",
      ticket: "ticket-abc",
      email: "brand.new@example.com",
    });
  });
});

describe("AuthApi.signupGoogle — FR-MDL-001's one question, post-Google", () => {
  it("posts the ticket and snake_case fields, and maps the response back", async () => {
    const fetchImpl = respond(200, {
      access_token: "tok",
      token_type: "bearer",
      mfa_verified: false,
      mfa: { has_passkey: false, has_totp: false },
    });

    const result = await api(fetchImpl).signupGoogle({
      ticket: "ticket-abc",
      accountModel: "firm",
      organizationName: "Bakker Accountants",
      kvkNumber: "87654321",
    });

    expect(result).toEqual({
      accessToken: "tok",
      mfaVerified: false,
      enrollment: { hasPasskey: false, hasTotp: false },
    });
    const [url, init] = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/v1/auth/signup/google");
    expect(JSON.parse(String(init.body))).toEqual({
      ticket: "ticket-abc",
      account_model: "firm",
      organization_name: "Bakker Accountants",
      kvk_number: "87654321",
    });
  });

  it("carries no Authorization header - the ticket is the credential", async () => {
    const fetchImpl = respond(200, {
      access_token: "tok",
      token_type: "bearer",
      mfa_verified: false,
    });
    await api(fetchImpl).signupGoogle({
      ticket: "ticket-abc",
      accountModel: "self_managed",
      organizationName: "Bakker",
      kvkNumber: null,
    });
    expect(
      headersOf(fetchImpl as unknown as ReturnType<typeof vi.fn>).Authorization,
    ).toBeUndefined();
  });
});
