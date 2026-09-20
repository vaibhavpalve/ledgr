/**
 * The signup/login/MFA screens' calls to `apps/api/src/api/auth/routes.py`
 * (ADR-054, IAM-010/IAM-011/IAM-012). Follows `capture/api.ts`/`client/api.ts`'s
 * conventions exactly: `ApiOptions`, `ApiError`, `OfflineError`,
 * `languageHeaders`, a fresh `Idempotency-Key` per mutating call (NFR-032 —
 * sent unconditionally here rather than only on the routes that need it,
 * since `api.idempotency_middleware` simply ignores the header on the paths
 * that carry no tenant context yet, and always sending it is one code path
 * instead of two).
 *
 * --- The one thing this client does that the others don't: `Authorization` ---
 *
 * Every endpoint here except `signup`/`login`/the two `login*Begin/Finish`
 * legs and the two `loginGoogle*` legs needs a bearer token — the MFA
 * enrolment/verification surface runs with a real, tenant-scoped session
 * that just hasn't cleared the MFA gate yet (see ADR-054's two-exemption-list
 * decision). `authHeaders()` from `./session` reads whatever this browser
 * currently has stored; callers never pass a token explicitly, the same way
 * no caller of `client/api.ts` passes an administration id explicitly to a
 * request that reads it from a header.
 */

import type { Language } from "@ledgr/i18n";

import { languageHeaders } from "../i18n";
import { authHeaders, readTrustedDeviceToken } from "./session";

export class OfflineError extends Error {}

/** See `capture/api.ts`'s identical class for the full rationale. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string | null,
    message: string,
  ) {
    super(message);
  }
}

export interface ApiOptions {
  language: () => Language;
  fetchImpl?: typeof fetch;
}

export type AccountModel = "self_managed" | "firm";

export interface MfaEnrollmentStatus {
  hasPasskey: boolean;
  hasTotp: boolean;
}

export interface AuthResult {
  accessToken: string;
  mfaVerified: boolean;
  /** Present only when `mfaVerified` is false - see `_auth_response` in
   * `api.auth.routes`. */
  enrollment: MfaEnrollmentStatus | null;
  /** ADR-061: present only when this response is an MFA verification that
   * was asked to remember the device (`remember_device: true`) - never on
   * login()'s response, which only ever CONSUMES a stored token, never
   * mints one. `./session`'s `storeTrustedDeviceToken` is where this ends
   * up; `AuthProvider.handleAuthResult` is the one caller that does that. */
  trustedDeviceToken: string | null;
}

export interface PasskeyChallenge {
  ceremonyId: string;
  optionsJson: string;
}

export type GoogleCallbackResult =
  | { kind: "signed_in"; result: AuthResult }
  | { kind: "link_required"; message: string }
  /** FR-MDL-001: this Google identity matched no existing account.
   * GoogleSignInService.sign_in already created a bare user and linked the
   * identity server-side (see api.auth.routes' module docstring) - `ticket`
   * is the one-time reference `signupGoogle` needs to finish the rest. */
  | { kind: "signup_required"; ticket: string; email: string };

interface AuthResponseJson {
  access_token: string;
  token_type: string;
  mfa_verified: boolean;
  mfa?: { has_passkey: boolean; has_totp: boolean };
  trusted_device_token?: string;
}

function toAuthResult(raw: AuthResponseJson): AuthResult {
  return {
    accessToken: raw.access_token,
    mfaVerified: raw.mfa_verified,
    enrollment: raw.mfa ? { hasPasskey: raw.mfa.has_passkey, hasTotp: raw.mfa.has_totp } : null,
    trustedDeviceToken: raw.trusted_device_token ?? null,
  };
}

export class AuthApi {
  constructor(private readonly options: ApiOptions) {}

  signup(params: {
    accountModel: AccountModel;
    organizationName: string;
    kvkNumber: string | null;
    email: string;
    password: string;
  }): Promise<AuthResult> {
    return this.call<AuthResponseJson>("POST", "/v1/auth/signup", {
      account_model: params.accountModel,
      organization_name: params.organizationName,
      kvk_number: params.kvkNumber,
      email: params.email,
      password: params.password,
    }).then(toAuthResult);
  }

  login(email: string, password: string): Promise<AuthResult> {
    // ADR-061: whatever this browser was last remembered on, if anything -
    // login() itself decides (server-side) whether it is still valid, and
    // silently ignores it otherwise, so there is no branching needed here.
    const trustedDeviceToken = readTrustedDeviceToken();
    return this.call<AuthResponseJson>("POST", "/v1/auth/login", {
      email,
      password,
      ...(trustedDeviceToken ? { trusted_device_token: trustedDeviceToken } : {}),
    }).then(toAuthResult);
  }

  /** IAM-018: a new password on proof of an authenticator code. Signs no one in;
   * the caller sends the person to the login screen. */
  recover(email: string, code: string, newPassword: string): Promise<void> {
    return this.call("POST", "/v1/auth/recover", {
      email,
      code,
      new_password: newPassword,
    }).then(() => undefined);
  }

  logout(): Promise<void> {
    return this.call("POST", "/v1/auth/logout", undefined, { authenticated: true }).then(
      () => undefined,
    );
  }

  loginPasskeyBegin(): Promise<PasskeyChallenge> {
    return this.call<{ ceremony_id: string; options: string }>(
      "POST",
      "/v1/auth/login/passkey/begin",
    ).then((raw) => ({ ceremonyId: raw.ceremony_id, optionsJson: raw.options }));
  }

  loginPasskeyFinish(ceremonyId: string, credential: Record<string, unknown>): Promise<AuthResult> {
    return this.call<AuthResponseJson>("POST", "/v1/auth/login/passkey/finish", {
      ceremony_id: ceremonyId,
      credential,
    }).then(toAuthResult);
  }

  loginGoogleStart(): Promise<{ authorizationUrl: string }> {
    return this.call<{ authorization_url: string }>("POST", "/v1/auth/login/google/start").then(
      (raw) => ({ authorizationUrl: raw.authorization_url }),
    );
  }

  async loginGoogleCallback(code: string, state: string): Promise<GoogleCallbackResult> {
    const raw = await this.call<
      | AuthResponseJson
      | { status: "link_required"; message: string }
      | { status: "signup_required"; ticket: string; email: string }
    >("POST", "/v1/auth/login/google/callback", { code, state });
    if ("status" in raw && raw.status === "link_required") {
      return { kind: "link_required", message: raw.message };
    }
    if ("status" in raw && raw.status === "signup_required") {
      return { kind: "signup_required", ticket: raw.ticket, email: raw.email };
    }
    return { kind: "signed_in", result: toAuthResult(raw as AuthResponseJson) };
  }

  /** FR-MDL-001's one question, for the Google identity `ticket` names -
   * see GoogleCallbackResult's `signup_required` variant. Never creates a
   * user itself (that already happened server-side); only the organization
   * and founding grant. */
  signupGoogle(params: {
    ticket: string;
    accountModel: AccountModel;
    organizationName: string;
    kvkNumber: string | null;
  }): Promise<AuthResult> {
    return this.call<AuthResponseJson>("POST", "/v1/auth/signup/google", {
      ticket: params.ticket,
      account_model: params.accountModel,
      organization_name: params.organizationName,
      kvk_number: params.kvkNumber,
    }).then(toAuthResult);
  }

  mfaTotpEnrollBegin(): Promise<{ secret: string; provisioningUri: string }> {
    return this.call<{ secret: string; provisioning_uri: string }>(
      "POST",
      "/v1/auth/mfa/totp/enroll/begin",
      undefined,
      { authenticated: true },
    ).then((raw) => ({ secret: raw.secret, provisioningUri: raw.provisioning_uri }));
  }

  mfaTotpEnrollConfirm(secret: string, code: string): Promise<AuthResult> {
    return this.call<AuthResponseJson>(
      "POST",
      "/v1/auth/mfa/totp/enroll/confirm",
      { secret, code },
      { authenticated: true },
    ).then(toAuthResult);
  }

  mfaTotpVerify(code: string, rememberDevice = false): Promise<AuthResult> {
    return this.call<AuthResponseJson>(
      "POST",
      "/v1/auth/mfa/totp/verify",
      { code, remember_device: rememberDevice },
      { authenticated: true },
    ).then(toAuthResult);
  }

  mfaPasskeyEnrollBegin(): Promise<PasskeyChallenge> {
    return this.call<{ ceremony_id: string; options: string }>(
      "POST",
      "/v1/auth/mfa/passkey/enroll/begin",
      undefined,
      { authenticated: true },
    ).then((raw) => ({ ceremonyId: raw.ceremony_id, optionsJson: raw.options }));
  }

  mfaPasskeyEnrollFinish(
    ceremonyId: string,
    name: string,
    credential: Record<string, unknown>,
  ): Promise<AuthResult> {
    return this.call<AuthResponseJson>(
      "POST",
      "/v1/auth/mfa/passkey/enroll/finish",
      { ceremony_id: ceremonyId, name, credential },
      { authenticated: true },
    ).then(toAuthResult);
  }

  mfaPasskeyVerifyBegin(): Promise<PasskeyChallenge> {
    return this.call<{ ceremony_id: string; options: string }>(
      "POST",
      "/v1/auth/mfa/passkey/verify/begin",
      undefined,
      { authenticated: true },
    ).then((raw) => ({ ceremonyId: raw.ceremony_id, optionsJson: raw.options }));
  }

  mfaPasskeyVerifyFinish(
    ceremonyId: string,
    credential: Record<string, unknown>,
    rememberDevice = false,
  ): Promise<AuthResult> {
    return this.call<AuthResponseJson>(
      "POST",
      "/v1/auth/mfa/passkey/verify/finish",
      { ceremony_id: ceremonyId, credential, remember_device: rememberDevice },
      { authenticated: true },
    ).then(toAuthResult);
  }

  private async call<T>(
    method: string,
    path: string,
    body?: unknown,
    opts: { authenticated?: boolean } = {},
  ): Promise<T> {
    const fetchImpl = this.options.fetchImpl ?? fetch;

    const headers: Record<string, string> = {
      ...languageHeaders(this.options.language()),
      ...(opts.authenticated ? authHeaders() : {}),
    };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      // NFR-032 - see this module's own docstring for why every mutating
      // call carries this rather than only the ones that strictly need it.
      headers["Idempotency-Key"] = crypto.randomUUID();
    }

    let response: Response;
    try {
      response = await fetchImpl(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      throw new OfflineError(`${method} ${path} could not reach the API`, { cause });
    }

    if (!response.ok) throw await problemFrom(response);
    return (await response.json()) as T;
  }
}

async function problemFrom(response: Response): Promise<ApiError> {
  let reason: string | null = null;
  let message = "";
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail !== null && typeof detail === "object") {
      const fields = detail as { reason?: unknown; message?: unknown };
      if (typeof fields.reason === "string") reason = fields.reason;
      if (typeof fields.message === "string") message = fields.message;
    }
  } catch {
    // A gateway's HTML or an empty body. The status is still the answer.
  }
  return new ApiError(response.status, reason, message || `HTTP ${response.status}`);
}
