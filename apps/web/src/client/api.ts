/**
 * The client identity header's call to the API — FR-FRM-000a.
 *
 * One endpoint, one call: `GET /v1/switcher/active`. Follows
 * `capture/api.ts`/`home/api.ts`'s conventions exactly (`ApiOptions`,
 * `ApiError`, `OfflineError`, headers from `languageHeaders`) — the same kind
 * of client talking to the same kind of server. Nothing here mutates, so
 * there is no idempotency key to attach.
 *
 * --- The one thing this client does that the others don't: it renames its
 * response ---
 *
 * Every other API client in this codebase (`ExpenseView`, `SalesInvoiceView`,
 * `InvoiceTemplateView`, ...) keeps its `@ledgr/shared-types` interface in
 * the wire's own snake_case, because those types are placed on screen more or
 * less as received. `ClientBadge` is the exception: its own doc comment says
 * its field names "match [`api.main._entry_json`'s] payload exactly", which
 * is a claim about the DATA (one shape, produced once), not the spelling —
 * `ClientBadge` is camelCase because `ClientHeader` and `ClientSwitcher`
 * already consume it that way. `toBadge` below is the one place the
 * translation from the wire's snake_case to that shape happens, so nothing
 * downstream — not `ClientHeader`, not a future mobile client — ever sees the
 * wire's own field names.
 *
 * --- `null` is data, not a failure ---
 *
 * `GET /v1/switcher/active`'s own handler docstring is explicit: it returns
 * `null` "when the session is not inside any client, which is a real state
 * ... and not an error." `fetchActiveBadge` passes that straight through as a
 * resolved `null`, exactly like every other real, non-error answer. It is
 * `AuthenticatedMobileShell`, not this client, that additionally tracks
 * whether the call has answered AT ALL (see its own `undefined` state) —
 * this method only ever returns what it actually received.
 */

import type { ClientBadge, ClientColour } from "@ledgr/shared-types";
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

/**
 * `api.main._entry_json`'s wire shape, before `toBadge` renames it into
 * `ClientBadge`. `role`/`role_is_system`/`expires_at` are also on the wire
 * (`SwitcherEntry` extends `ClientBadge` server-side) but `ClientHeader` has
 * no use for them, so they are not part of `ClientBadge` and are not read
 * here.
 */
interface ActiveBadgeResponse {
  administration_id: string;
  display_name: string;
  legal_name: string;
  trade_name: string | null;
  kvk_number: string | null;
  colour: ClientColour;
  initials: string;
  colour_is_ambiguous: boolean;
}

export class ClientApi {
  constructor(private readonly options: ApiOptions) {}

  /**
   * FR-FRM-000a: what `ClientHeader` renders. Resolves to `null` when the API
   * reports no active administration (see this module's docstring) — that is
   * a confirmed, real answer, not this method's way of representing "not
   * fetched yet".
   */
  async fetchActiveBadge(): Promise<ClientBadge | null> {
    const raw = await this.call<ActiveBadgeResponse | null>("GET", "/v1/switcher/active");
    return raw === null ? null : toBadge(raw);
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

function toBadge(raw: ActiveBadgeResponse): ClientBadge {
  return {
    administrationId: raw.administration_id,
    displayName: raw.display_name,
    legalName: raw.legal_name,
    tradeName: raw.trade_name,
    kvkNumber: raw.kvk_number,
    colour: raw.colour,
    initials: raw.initials,
    colourIsAmbiguous: raw.colour_is_ambiguous,
  };
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
