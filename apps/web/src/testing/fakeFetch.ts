import { vi } from "vitest";

/**
 * A `fetch` routed by `METHOD path` for the client tests written against the
 * §4 contract. Records every call so a test can assert the exact wire shape
 * — the body, the idempotency key, the Accept-Language — rather than only the
 * decoded result.
 */
export interface RecordedCall {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function problemResponse(status: number, reason: string, message: string, extra = {}): Response {
  return jsonResponse({ detail: { reason, message, ...extra } }, status);
}

export function fakeFetch(handlers: Record<string, (call: RecordedCall) => Response | Promise<Response>>) {
  const calls: RecordedCall[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((value, key) => {
      headers[key.toLowerCase()] = value;
    });
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : (init?.body ?? null);
    const call: RecordedCall = { method, url, headers, body };
    calls.push(call);
    const exact = handlers[`${method} ${url}`];
    if (exact) return exact(call);
    const byPath = handlers[`${method} ${url.split("?")[0] ?? url}`];
    if (byPath) return byPath(call);
    return new Response(JSON.stringify({ detail: `no handler for ${method} ${url}` }), {
      status: 500,
    });
  }) as unknown as typeof fetch;
  return { impl, calls };
}
