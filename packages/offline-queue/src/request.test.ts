import { describe, expect, it } from "vitest";

import type { SealedPayload } from "./model";
import { buildRequest, classify } from "./request";

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
  idempotencyKey: "key-abc",
  capturedAt: "2026-09-06T10:00:00.000Z",
};

const image = new Uint8Array([1, 2, 3]);

describe("buildRequest", () => {
  it("posts to the administration the capture was taken for", () => {
    const request = buildRequest(payload, image);

    expect(request.method).toBe("POST");
    expect(request.path).toContain("/v1/administrations/adm-A/capture-sessions/sess-1/pages");
  });

  it("carries the capture's OWN tenant context, not whatever client is open now", () => {
    // The client-side half of CLAUDE.md's first rule. A receipt photographed
    // for one client and uploaded twenty minutes later, after the person has
    // switched to another, still posts to the client it was photographed for —
    // and the only way to get that wrong is to read an ambient value, which is
    // why this function takes none.
    const forB = buildRequest({ ...payload, administrationId: "adm-B" }, image);

    expect(forB.path).toContain("/v1/administrations/adm-B/");
    expect(forB.path).not.toContain("adm-A");
  });

  it("sends the chosen category with the page that creates the receipt", () => {
    const request = buildRequest({ ...payload, category: "office_supplies" }, image);

    expect(new URL(request.path, "http://x").searchParams.get("category")).toBe("office_supplies");
  });

  it("sends no category for a receipt that has none, or for a later page", () => {
    const none = buildRequest({ ...payload, category: null }, image);
    const later = buildRequest({ ...payload, pageIndex: 1, category: "lunch" }, image, {
      resolvedItemId: "item-1",
    });

    expect(new URL(none.path, "http://x").searchParams.has("category")).toBe(false);
    expect(new URL(later.path, "http://x").searchParams.has("category")).toBe(false);
  });

  it("sends the same idempotency key every time, which is what stops a double post", () => {
    // NFR-032. A key regenerated per attempt would double-post exactly the
    // receipt whose first response was lost on a flapping connection.
    const first = buildRequest(payload, image);
    const retry = buildRequest(payload, image);

    expect(first.headers["Idempotency-Key"]).toBe("key-abc");
    expect(retry.headers["Idempotency-Key"]).toBe("key-abc");
  });

  it("sends the fiscal year and the source", () => {
    const request = buildRequest(payload, image);

    expect(request.path).toContain("fiscal_year_id=fy-2026");
    expect(request.path).toContain("source=camera");
  });

  it("omits `item` for the page that opens a receipt", () => {
    // ADR-031 §3, and the difference between one expense and three. A page-0
    // request must never carry an item, even if one is somehow to hand:
    // carrying one would attach the opening page of a new receipt to a
    // different receipt entirely.
    expect(buildRequest(payload, image).path).not.toContain("item=");
    expect(buildRequest(payload, image, { resolvedItemId: "item-7" }).path).not.toContain("item=");
  });

  it("sends `item` for a further page, from the id the server gave page 0", () => {
    const secondPage = { ...payload, pageIndex: 1 };

    expect(buildRequest(secondPage, image, { resolvedItemId: "item-7" }).path).toContain(
      "item=item-7",
    );
  });

  it("omits `item` for a further page that has not been resolved yet", () => {
    // Belt and braces. `policy.due` will not offer such a record at all; if one
    // ever reached here it must not open a second receipt for half an invoice.
    expect(buildRequest({ ...payload, pageIndex: 1 }, image).path).not.toContain("item=");
  });

  it("escapes a file name rather than pasting it into the query", () => {
    const request = buildRequest({ ...payload, filename: "bon 12&13.jpg" }, image);

    expect(request.path).toContain("filename=bon+12%2613.jpg");
  });

  it("declares the content type it was captured with", () => {
    expect(
      buildRequest({ ...payload, contentType: "application/pdf" }, image).headers["Content-Type"],
    ).toBe("application/pdf");
  });

  it("carries caller headers but cannot have them overwrite the idempotency key", () => {
    const request = buildRequest(payload, image, {
      headers: { Authorization: "Bearer t", "Idempotency-Key": "smuggled" },
    });

    expect(request.headers["Authorization"]).toBe("Bearer t");
    expect(request.headers["Idempotency-Key"]).toBe("key-abc");
  });

  it("sends the image as the body, unwrapped", () => {
    // The endpoint takes raw bytes with Content-Type describing them — no
    // multipart parser over untrusted input (api.expenses.routes).
    expect(buildRequest(payload, image).body).toBe(image);
  });
});

describe("classify", () => {
  const response = (status: number, reason: string | null = null) =>
    classify({ kind: "response", status, reason, itemId: null });

  it("treats a created page as delivered", () => {
    expect(response(201)).toEqual({ kind: "delivered" });
    expect(response(200)).toEqual({ kind: "delivered" });
  });

  it("retries a request that never reached a server", () => {
    expect(classify({ kind: "network_error" })).toEqual({ kind: "retry" });
  });

  it.each([
    ["unsupported_document_type", "unsupported_type"],
    ["document_infected", "infected"],
    ["document_too_large", "too_large_for_server"],
    ["capture_session_closed", "session_closed"],
    ["capture_session_not_found", "not_found"],
    ["capture_item_not_found", "not_found"],
  ])("blocks on %s, which no retry can change", (reason, blocked) => {
    expect(response(409, reason)).toEqual({ kind: "blocked", reason: blocked });
  });

  it("separates the two different 409s", () => {
    // A finalised capture session is permanent; the idempotency middleware's
    // "a request with this key is executing right now" carries no reason and
    // asks for exactly the retry it gets.
    expect(response(409, "capture_session_closed").kind).toBe("blocked");
    expect(response(409, null)).toEqual({ kind: "retry" });
  });

  it("retries while the malware scanner is unavailable", () => {
    // SEC-005's scan being down is a refusal of the moment, not of the bytes.
    expect(response(503, "scan_unavailable")).toEqual({ kind: "retry" });
  });

  it("retries an expired access token rather than blocking the receipt", () => {
    // The token refreshes; the receipt should go when it does. A session that
    // ends for real ends in a purge, which removes the record entirely.
    expect(response(401)).toEqual({ kind: "retry" });
  });

  it("blocks when the person may not submit expenses", () => {
    expect(response(403)).toEqual({ kind: "blocked", reason: "not_permitted" });
  });

  it("retries a rate limit and a server error", () => {
    expect(response(429)).toEqual({ kind: "retry" });
    expect(response(500)).toEqual({ kind: "retry" });
    expect(response(503)).toEqual({ kind: "retry" });
  });

  it("blocks an oversized upload the archive refused", () => {
    expect(response(413)).toEqual({ kind: "blocked", reason: "too_large_for_server" });
    expect(response(413, "document_too_large")).toEqual({
      kind: "blocked",
      reason: "too_large_for_server",
    });
  });

  it("blocks rather than discards an unrecognised refusal", () => {
    // A queue that quietly dropped what it did not understand is how a receipt
    // disappears. Blocked is visible and reversible; discarded is neither.
    expect(response(418)).toEqual({ kind: "blocked", reason: "rejected" });
    expect(response(422)).toEqual({ kind: "blocked", reason: "rejected" });
  });
});
