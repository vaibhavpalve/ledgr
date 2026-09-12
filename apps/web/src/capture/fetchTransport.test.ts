import { describe, expect, it, vi } from "vitest";
import { buildRequest, classify } from "@ledgr/offline-queue";
import type { SealedPayload } from "@ledgr/offline-queue";

import { FetchTransport } from "./fetchTransport";

const payload: SealedPayload = {
  organizationId: "org-1",
  administrationId: "adm-A",
  fiscalYearId: "fy-2026",
  userId: "user-1",
  sessionId: "sess-1",
  receiptRef: "receipt-1",
  pageIndex: 0,
  source: "camera",
  filename: null,
  contentType: "image/jpeg",
  idempotencyKey: "key-1",
  capturedAt: "2026-09-06T10:00:00.000Z",
};

const request = buildRequest(payload, new Uint8Array([1, 2, 3]), {
  headers: { "Accept-Language": "nl" },
});

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

describe("FetchTransport", () => {
  it("posts the raw bytes with the headers the request builder set", async () => {
    const fetchImpl = respond(201, { item_id: "item-1" });

    await new FetchTransport(fetchImpl).send(request);

    expect(fetchImpl).toHaveBeenCalledWith(
      request.path,
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "image/jpeg",
          "Idempotency-Key": "key-1",
          "Accept-Language": "nl",
        }),
      }),
    );
  });

  it("reports a status, which the shared classifier turns into a decision", async () => {
    const response = await new FetchTransport(respond(201, {})).send(request);

    expect(response).toEqual({ kind: "response", status: 201, reason: null, itemId: null });
    expect(classify(response)).toEqual({ kind: "delivered" });
  });

  it("carries back the item id, which is how a receipt's later pages find it", async () => {
    // ADR-031 §3: the server allocates the item when page 0 lands, and every
    // further page of that receipt has to quote it. Without this the queue
    // could only ever send single-page receipts offline.
    const body = { item_id: "item-9", document_id: "doc-1", page_number: 1 };

    const response = await new FetchTransport(respond(201, body)).send(request);

    expect(response).toEqual({ kind: "response", status: 201, reason: null, itemId: "item-9" });
  });

  it("reports no item id on a refusal", async () => {
    const response = await new FetchTransport(
      respond(415, { detail: { reason: "unsupported_document_type" } }),
    ).send(request);

    expect(response.kind === "response" && response.itemId).toBeNull();
  });

  it("lifts out the machine-readable reason, never the sentence", async () => {
    // The `message` is prose already translated for a person and would break
    // the first time it was reworded; `reason` is the field to branch on.
    const body = {
      detail: { message: "Deze reeks is al afgerond.", reason: "capture_session_closed" },
    };

    const response = await new FetchTransport(respond(409, body)).send(request);

    expect(response).toEqual({
      kind: "response",
      status: 409,
      reason: "capture_session_closed",
      itemId: null,
    });
    expect(classify(response)).toEqual({ kind: "blocked", reason: "session_closed" });
  });

  it("reports no reason for a body that carries none, so a conflict retries", async () => {
    // The idempotency middleware's 409 is a plain string detail. Reading no
    // reason from it is what makes it retry rather than block.
    const response = await new FetchTransport(
      respond(409, { detail: "a request is executing" }),
    ).send(request);

    expect(response).toEqual({ kind: "response", status: 409, reason: null, itemId: null });
    expect(classify(response)).toEqual({ kind: "retry" });
  });

  it("survives a body that is not JSON at all", async () => {
    const fetchImpl = vi.fn(
      async () => new Response("<html>gateway</html>", { status: 502 }),
    ) as unknown as typeof fetch;

    const response = await new FetchTransport(fetchImpl).send(request);

    expect(response).toEqual({ kind: "response", status: 502, reason: null, itemId: null });
    expect(classify(response)).toEqual({ kind: "retry" });
  });

  it("reports a phone in a tunnel as a network error rather than throwing", async () => {
    // The ordinary case for this queue, and a fact about the world rather than
    // an exception to propagate.
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;

    const response = await new FetchTransport(fetchImpl).send(request);

    expect(response).toEqual({ kind: "network_error" });
    expect(classify(response)).toEqual({ kind: "retry" });
  });
});
