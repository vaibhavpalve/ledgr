/**
 * Turning a queued capture into a request, and a response into a decision —
 * FR-EXP-001f, NFR-032.
 *
 * Both halves are pure and both live here rather than in each platform's
 * transport. What a 415 means is API knowledge; two transports classifying it
 * independently is two chances for one of them to retry forever something the
 * server will never accept.
 *
 * --- The tenant context comes from the RECORD ---
 *
 * `buildRequest` reads the administration id out of the sealed payload and
 * nowhere else. There is no "current administration" parameter and there must
 * not be: a receipt photographed for one client and uploaded twenty minutes
 * later, after the person has switched to another, has to post to the client
 * it was photographed for. CLAUDE.md's first rule is usually read as a
 * server-side obligation; this is the client-side half of it, and it is the
 * one place a queue can silently get it wrong.
 */

import type { BlockedReason, Bytes, SealedPayload } from "./model";
import type { CaptureRequest, TransportResponse } from "./ports";

/**
 * The single capture endpoint (ADR-031 §2). One path for camera and upload
 * alike — `source` is a label on the row, not a route.
 */
export function buildRequest(
  payload: SealedPayload,
  image: Bytes,
  {
    resolvedItemId = null,
    headers = {},
  }: { resolvedItemId?: string | null; headers?: Readonly<Record<string, string>> } = {},
): CaptureRequest {
  const query = new URLSearchParams({
    fiscal_year_id: payload.fiscalYearId,
    source: payload.source,
  });
  // ADR-031 §3: present means "another original for a receipt already
  // captured", absent means "a new receipt". Omitted rather than sent empty —
  // an empty `item` would be a malformed uuid, not a missing one.
  //
  // Passed in rather than read off the payload, because it is not known when
  // the payload is sealed: the server allocates it as page 0 lands, and pages 1
  // and 2 of an invoice were photographed before that happened. A page-0
  // request never carries one, which is what makes it create the receipt.
  if (payload.pageIndex > 0 && resolvedItemId !== null) query.set("item", resolvedItemId);
  if (payload.filename !== null) query.set("filename", payload.filename);

  return {
    method: "POST",
    path:
      `/v1/administrations/${encodeURIComponent(payload.administrationId)}` +
      `/capture-sessions/${encodeURIComponent(payload.sessionId)}/pages?${query.toString()}`,
    headers: {
      ...headers,
      "Content-Type": payload.contentType,
      // NFR-032. The SAME key on every attempt of this capture — that is what
      // makes a response lost mid-flight safe to retry rather than the thing
      // that posts the receipt twice.
      "Idempotency-Key": payload.idempotencyKey,
    },
    body: image,
  };
}

export type Classification =
  | { readonly kind: "delivered" }
  | { readonly kind: "retry" }
  | { readonly kind: "blocked"; readonly reason: BlockedReason };

/**
 * What to do with what came back.
 *
 * The dividing line is not "did it work" but "could offering it again ever
 * work". Everything transient — no network, a 5xx, a rate limit, an expired
 * access token the app will refresh — is `retry`, with no attempt limit
 * (policy.backoffMs explains why). Everything the server has judged about
 * THESE BYTES or THIS SESSION is `blocked`: it is put in front of a person
 * with a reason, because no amount of waiting changes an unsupported format.
 *
 * `reason` is the machine-readable field `api.i18n.http.problem` puts beside
 * the sentence, and reading it is what separates two different 409s: a capture
 * session that has been finalised (permanent) from the idempotency
 * middleware's "a request with this key is executing right now" (transient,
 * and the retry is precisely what it asks for).
 */
export function classify(response: TransportResponse): Classification {
  if (response.kind === "network_error") return { kind: "retry" };

  const { status, reason } = response;

  if (status >= 200 && status < 300) return { kind: "delivered" };

  switch (reason) {
    case "unsupported_document_type":
      return { kind: "blocked", reason: "unsupported_type" };
    case "document_infected":
      return { kind: "blocked", reason: "infected" };
    case "document_too_large":
      return { kind: "blocked", reason: "too_large_for_server" };
    case "capture_session_closed":
      return { kind: "blocked", reason: "session_closed" };
    case "capture_session_not_found":
    case "capture_item_not_found":
      return { kind: "blocked", reason: "not_found" };
    // The archive's scanner being down is the one 5xx-shaped refusal that
    // arrives with a reason, and it is temporary by definition.
    case "scan_unavailable":
      return { kind: "retry" };
    default:
      break;
  }

  // 401 is retried on purpose. An access token that expired while the phone
  // was in a tunnel is the ordinary case, the app refreshes it, and the
  // receipt should go when it does. A session that ends for real ends in a
  // purge (MOB-009), which removes the record rather than leaving it retrying.
  if (status === 401) return { kind: "retry" };
  if (status === 403) return { kind: "blocked", reason: "not_permitted" };
  if (status === 404) return { kind: "blocked", reason: "not_found" };
  // A 409 carrying no `reason` is the idempotency middleware saying a request
  // with this key is executing right now. Retrying shortly is its own advice.
  if (status === 409) return { kind: "retry" };
  if (status === 413) return { kind: "blocked", reason: "too_large_for_server" };
  if (status === 429 || status >= 500) return { kind: "retry" };

  // Any other 4xx is the client being wrong about something, which retrying
  // will not fix. Surfaced rather than swallowed: a queue quietly discarding
  // what it did not understand is how a receipt disappears.
  if (status >= 400) return { kind: "blocked", reason: "rejected" };

  return { kind: "retry" };
}
