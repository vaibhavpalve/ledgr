/**
 * One capture upload over `fetch` — FR-EXP-001f.
 *
 * Everything with a judgement in it is elsewhere: `buildRequest` decides the
 * path and the headers, `classify` decides what a status means. This lifts the
 * `reason` out of the body and hands both back, and does no interpreting of
 * its own — a transport that decided for itself which failures were permanent
 * would be a second copy of that decision, and the copy that drifted would be
 * the one retrying an unsupported format forever.
 *
 * A thrown `fetch` is a `network_error` rather than an exception. That is the
 * ordinary case for this queue — a phone in a tunnel — and it is a fact about
 * the world, not a bug to propagate.
 */

import type { CaptureRequest, Transport, TransportResponse } from "@ledgr/offline-queue";

export class FetchTransport implements Transport {
  constructor(private readonly fetchImpl: typeof fetch = fetch) {}

  async send(request: CaptureRequest): Promise<TransportResponse> {
    let response: Response;
    try {
      response = await this.fetchImpl(request.path, {
        method: request.method,
        headers: request.headers,
        // The endpoint takes raw bytes with Content-Type describing them, as
        // api.documents.routes.upload_document does — no multipart parser over
        // untrusted input.
        body: request.body,
      });
    } catch {
      return { kind: "network_error" };
    }

    const body = await bodyOf(response);
    return {
      kind: "response",
      status: response.status,
      reason: stringField(body, "reason") ?? stringField(detailOf(body), "reason"),
      // The receipt this page landed on, which the queue needs before it can
      // send that receipt's remaining pages (ADR-031 §3).
      itemId: response.ok ? stringField(body, "item_id") : null,
    };
  }
}

async function bodyOf(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    // A gateway's HTML, an empty 204, a truncated response. All mean "no
    // fields to read", which every caller here already handles as null.
    return null;
  }
}

/**
 * `api.i18n.http.problem` puts its fields under `detail`; FastAPI's own
 * validation errors put a different shape there. Reading both levels is why
 * this is a helper rather than one path — and it is always the machine-readable
 * `reason`, never the `message`, which is prose for a person and would break
 * the first time it was reworded.
 */
function detailOf(body: unknown): unknown {
  return (body as { detail?: unknown } | null)?.detail;
}

function stringField(source: unknown, name: string): string | null {
  const value = (source as Record<string, unknown> | null)?.[name];
  return typeof value === "string" ? value : null;
}
