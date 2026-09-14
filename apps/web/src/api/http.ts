/**
 * The one fetch-and-refuse helper the clients built for the routed app share
 * — `account/api.ts`, `onboarding/api.ts`, `customers/api.ts`, `ledger/api.ts`.
 *
 * ADR-049 §4 recorded that `ApiError`/`OfflineError`/`problemFrom` were by
 * then copied verbatim into five client modules and deserved a shared home.
 * This is that home for every client written since; the five earlier ones
 * are left as they are (their tests and callers `instanceof` their own
 * classes), which is the same "do not refactor four files as a side effect"
 * call ADR-049 made.
 *
 * --- What every call carries ---
 *
 *   Accept-Language   FR-UX-007: refusals come back in the language on screen.
 *   Idempotency-Key   NFR-032, on every mutating call. A fresh key per call,
 *                     because each is a new intention; retries live in the
 *                     offline queue, which keeps its own key per capture.
 *   Authorization     NOT added here. It is the injected `fetchImpl`'s job
 *                     (`session/authenticatedFetch.ts`) — one place that knows
 *                     where the token lives and what a 401 means, rather than
 *                     one per client.
 */

import type { Language } from "@ledgr/i18n";

import { languageHeaders } from "../i18n";

/** The call could not be made because there is no connection. */
export class OfflineError extends Error {}

/**
 * The API refused, with the machine-readable `reason` from
 * `api.i18n.http.problem` and the already-translated sentence beside it.
 * `message` is SHOWN (the server rendered it for the language asked for);
 * `reason` is what code branches on; `fields` carries whatever machine
 * context travelled beside the sentence (`missing_fields`, `field`, ...).
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string | null,
    message: string,
    readonly fields: Readonly<Record<string, unknown>> = {},
  ) {
    super(message);
  }
}

export interface ApiOptions {
  language: () => Language;
  fetchImpl?: typeof fetch;
}

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

/**
 * One JSON round trip. `fetchImpl` is read at call time rather than captured
 * at construction, so a test that stubs the global after building a client
 * still gets its stub, and the app's injected authenticated fetch is what
 * production always passes.
 */
export async function callJson<T>(
  options: ApiOptions,
  method: HttpMethod,
  path: string,
  body?: unknown,
): Promise<T> {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const headers: Record<string, string> = { ...languageHeaders(options.language()) };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["Idempotency-Key"] = crypto.randomUUID();

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
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function problemFrom(response: Response): Promise<ApiError> {
  let reason: string | null = null;
  let message = "";
  let fields: Record<string, unknown> = {};
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail !== null && typeof detail === "object") {
      const { reason: rawReason, message: rawMessage, ...rest } = detail as Record<string, unknown>;
      if (typeof rawReason === "string") reason = rawReason;
      if (typeof rawMessage === "string") message = rawMessage;
      fields = rest;
    }
  } catch {
    // A gateway's HTML or an empty body. The status is still the answer.
  }
  return new ApiError(response.status, reason, message || `HTTP ${response.status}`, fields);
}

/**
 * The sentence a screen shows for a failure it did not anticipate: the
 * server's own (already translated — FR-UX-007), or the network message when
 * there was no server response to translate. Screens that can say something
 * more specific (D5: what happened and what to do next) do so BEFORE falling
 * back to this.
 */
export function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

/** Whether a failure was the absence of a connection rather than a refusal. */
export function isOffline(error: unknown): boolean {
  return error instanceof OfflineError;
}

/**
 * A list that a route may send bare (`[...]`) or wrapped (`{ "<key>": [...] }`)
 * — the one shape difference this app tolerates from routes still being
 * built against §4, so a client's own tests can pin the decoded result
 * either way. `Array.isArray` narrows to `any[]`, which is why this is a
 * helper rather than an inline check at every call site.
 */
export function unwrapList<T>(raw: readonly T[] | object, key: string): T[] {
  if (Array.isArray(raw)) return [...(raw as readonly T[])];
  const inner = (raw as Record<string, unknown>)[key];
  return Array.isArray(inner) ? [...(inner as readonly T[])] : [];
}

/** `encodeURIComponent` over every segment, so an id never becomes a path. */
export function pathOf(...segments: readonly string[]): string {
  return "/" + segments.map((segment) => encodeURIComponent(segment)).join("/");
}

/** A query string from only the parameters that have a value. */
export function queryOf(params: Readonly<Record<string, string | number | null | undefined>>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered === "" ? "" : `?${rendered}`;
}
