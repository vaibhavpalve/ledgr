import { describe, expect, it } from "vitest";

import { CorruptEnvelope, frame, unframe } from "./envelope";
import type { SealedPayload } from "./model";

const payload: SealedPayload = {
  organizationId: "org-1",
  administrationId: "adm-1",
  fiscalYearId: "fy-2026",
  userId: "user-1",
  sessionId: "sess-1",
  receiptRef: "receipt-1",
  pageIndex: 0,
  source: "camera",
  filename: "receipt.jpg",
  contentType: "image/jpeg",
  idempotencyKey: "key-1",
  capturedAt: "2026-09-06T10:00:00.000Z",
};

describe("frame/unframe", () => {
  it("round-trips metadata and image as one envelope", () => {
    const image = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]);

    const { payload: back, image: imageBack } = unframe(frame(payload, image));

    expect(back).toEqual(payload);
    expect([...imageBack]).toEqual([...image]);
  });

  it("survives an image containing bytes that look like the header", () => {
    // A JPEG is arbitrary binary and will contain zero bytes, brace characters
    // and anything else. The length prefix is what makes that a non-issue, and
    // this is the test that says so.
    const image = new Uint8Array([0, 0, 0, 200, 0x7b, 0x7d, 0, 0]);

    expect([...unframe(frame(payload, image)).image]).toEqual([...image]);
  });

  it("round-trips an empty image", () => {
    expect(unframe(frame(payload, new Uint8Array())).image.length).toBe(0);
  });

  it("keeps the receipt reference and page index, which decide how many expenses are created", () => {
    // ADR-031 §3: page 0 opens a receipt and a draft expense, later pages join
    // it. Losing either field in the envelope would turn a three-page invoice
    // into three claims — silently, and in the direction that multiplies
    // somebody's money.
    const secondPage = { ...payload, receiptRef: "receipt-7", pageIndex: 2 };

    const { payload: back } = unframe(frame(secondPage, new Uint8Array([1])));

    expect(back.receiptRef).toBe("receipt-7");
    expect(back.pageIndex).toBe(2);
  });

  it("refuses an envelope shorter than its length prefix", () => {
    expect(() => unframe(new Uint8Array([0, 0]))).toThrow(CorruptEnvelope);
  });

  it("refuses a header length the envelope does not contain", () => {
    const truncated = frame(payload, new Uint8Array([1, 2, 3])).slice(0, 12);

    expect(() => unframe(truncated)).toThrow(CorruptEnvelope);
  });

  it("refuses an absurd header length rather than allocating for it", () => {
    const lying = new Uint8Array(16);
    new DataView(lying.buffer).setUint32(0, 0xffffff, false);

    expect(() => unframe(lying)).toThrow(CorruptEnvelope);
  });

  it("hands back an image that does not hold the metadata alive behind it", () => {
    // `slice`, not `subarray`. A view would keep the whole decrypted envelope
    // — including the administration id and the file name — reachable for as
    // long as the transport holds the body.
    const framed = frame(payload, new Uint8Array([9, 9, 9]));

    expect(unframe(framed).image.buffer.byteLength).toBe(3);
  });
});
