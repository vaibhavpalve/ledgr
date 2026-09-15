/**
 * The session bootstrap and account-settings calls —
 * docs/founder-review-2026-09-14.md §4.1 and §4.4, built against that
 * contract while the backend team lands the routes in parallel.
 *
 *   GET    /v1/me                      the one read that starts a session
 *   GET    /v1/me/sessions             IAM-017
 *   DELETE /v1/me/sessions/{id}
 *   GET    /v1/me/passkeys
 *   DELETE /v1/me/passkeys/{id}        IAM-010f's continuity guard applies
 *   GET    /v1/me/trusted-devices      ADR-061
 *   DELETE /v1/me/trusted-devices/{id}
 *   POST   /v1/me/password             re-authenticates, breach-checked
 *   POST   /v1/auth/verify-email       IAM-010b, `{ token }`
 *   POST   /v1/auth/verify-email/resend
 *
 * Shapes are `@ledgr/shared-types`' `MeView`/`SessionView`/`PasskeyView`,
 * placed on screen as received. Where the real backend answers differently
 * the reconciliation happens HERE, in this client, never by editing the
 * backend (the brief's own rule) — `toSessionView`/`toPasskeyView` are the
 * two seams already reserved for that.
 */

import type { MeView, PasskeyView, SessionView, TrustedDeviceView } from "@ledgr/shared-types";

import { callJson, pathOf, unwrapList, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export class AccountApi {
  constructor(private readonly options: ApiOptions) {}

  /** §4.1. Authorization-exempt server-side: it returns only the caller's own memberships. */
  getMe(): Promise<MeView> {
    return callJson<MeView>(this.options, "GET", "/v1/me");
  }

  async listSessions(): Promise<SessionView[]> {
    const raw = await callJson<readonly RawSession[] | { sessions: readonly RawSession[] }>(
      this.options,
      "GET",
      "/v1/me/sessions",
    );
    return unwrapList<RawSession>(raw, "sessions").map(toSessionView);
  }

  revokeSession(sessionId: string): Promise<void> {
    return callJson<void>(this.options, "DELETE", pathOf("v1", "me", "sessions", sessionId));
  }

  async listPasskeys(): Promise<PasskeyView[]> {
    const raw = await callJson<readonly RawPasskey[] | { passkeys: readonly RawPasskey[] }>(
      this.options,
      "GET",
      "/v1/me/passkeys",
    );
    return unwrapList<RawPasskey>(raw, "passkeys").map(toPasskeyView);
  }

  removePasskey(passkeyId: string): Promise<void> {
    return callJson<void>(this.options, "DELETE", pathOf("v1", "me", "passkeys", passkeyId));
  }

  async listTrustedDevices(): Promise<TrustedDeviceView[]> {
    const raw = await callJson<
      readonly TrustedDeviceView[] | { trusted_devices: readonly TrustedDeviceView[] }
    >(this.options, "GET", "/v1/me/trusted-devices");
    return unwrapList<TrustedDeviceView>(raw, "trusted_devices");
  }

  revokeTrustedDevice(deviceId: string): Promise<void> {
    return callJson<void>(this.options, "DELETE", pathOf("v1", "me", "trusted-devices", deviceId));
  }

  changePassword(currentPassword: string, newPassword: string): Promise<void> {
    return callJson<void>(this.options, "POST", "/v1/me/password", {
      current_password: currentPassword,
      new_password: newPassword,
    });
  }

  verifyEmail(token: string): Promise<void> {
    return callJson<void>(this.options, "POST", "/v1/auth/verify-email", { token });
  }

  resendVerificationEmail(): Promise<void> {
    return callJson<void>(this.options, "POST", "/v1/auth/verify-email/resend");
  }
}

/** Tolerant of the two spellings a session row might arrive in. */
interface RawSession {
  id: string;
  created_at: string;
  last_active_at?: string;
  last_seen_at?: string;
  expires_at: string;
  is_current?: boolean;
  current?: boolean;
}

function toSessionView(raw: RawSession): SessionView {
  return {
    id: raw.id,
    created_at: raw.created_at,
    last_active_at: raw.last_active_at ?? raw.last_seen_at ?? raw.created_at,
    expires_at: raw.expires_at,
    is_current: raw.is_current ?? raw.current ?? false,
  };
}

interface RawPasskey {
  id: string;
  name: string;
  created_at: string;
  last_used_at?: string | null;
}

function toPasskeyView(raw: RawPasskey): PasskeyView {
  return {
    id: raw.id,
    name: raw.name,
    created_at: raw.created_at,
    last_used_at: raw.last_used_at ?? null,
  };
}
