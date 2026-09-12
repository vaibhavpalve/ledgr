/**
 * The Home tab's call to the API — FR-UX-005, MOB-006.
 *
 * One endpoint, one call: `GET .../dashboard`. Follows `capture/api.ts` and
 * `invoicing/api.ts`'s conventions exactly (`ApiOptions`, `ApiError`,
 * `OfflineError`, headers from `languageHeaders`) — the same kind of client
 * talking to the same kind of server. There is no mutating call here, so
 * there is no idempotency key to attach.
 */

import type { DashboardView } from "@ledgr/shared-types";
import type { Language } from "@ledgr/i18n";

import { languageHeaders } from "../i18n";

/** The call could not be made because there is no connection. */
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

export class DashboardApi {
  constructor(private readonly options: ApiOptions) {}

  /**
   * FR-UX-005 / MOB-006. `fiscalYearId` is required — this dashboard has no
   * opinion about which year is "current" (there is no session/active-fiscal-
   * year concept yet, the same gap `MobileShell`'s own props already carry)
   * and asks the caller to say.
   */
  getDashboard(administrationId: string, fiscalYearId: string): Promise<DashboardView> {
    const path =
      `/v1/administrations/${encodeURIComponent(administrationId)}/dashboard` +
      `?fiscal_year_id=${encodeURIComponent(fiscalYearId)}`;
    return this.call<DashboardView>("GET", path);
  }

  private async call<T>(method: string, path: string): Promise<T> {
    const fetchImpl = this.options.fetchImpl ?? fetch;

    let response: Response;
    try {
      response = await fetchImpl(path, {
        method,
        headers: languageHeaders(this.options.language()),
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
