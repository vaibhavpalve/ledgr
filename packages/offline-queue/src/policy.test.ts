import { describe, expect, it } from "vitest";

import type { QueueState, StoredCapture } from "./model";
import { admit, backoffMs, due, nextDueAt, usedBytes } from "./policy";

function record(overrides: Partial<StoredCapture> & { id: string }): StoredCapture {
  return {
    state: "queued" as QueueState,
    receiptRef: `receipt-${overrides.id}`,
    pageIndex: 0,
    resolvedItemId: null,
    bytes: 1_000,
    capturedAt: "2026-09-06T10:00:00.000Z",
    attempts: 0,
    nextAttemptAt: null,
    lastAttemptAt: null,
    blockedReason: null,
    sealed: { iv: new Uint8Array(), ciphertext: new Uint8Array() },
    ...overrides,
  };
}

describe("admit — MOB-009's size cap", () => {
  it("accepts a capture that fits", () => {
    expect(admit({ used: 10, incoming: 5, cap: 20 })).toEqual({ accepted: true });
  });

  it("accepts a capture that exactly fills the cap", () => {
    expect(admit({ used: 15, incoming: 5, cap: 20 })).toEqual({ accepted: true });
  });

  it("refuses the NEW capture rather than evicting a queued one", () => {
    // The decision this whole module turns on. A queued receipt on an offline
    // phone is the only copy of the photograph; evicting it to make room would
    // destroy a document its owner believes is saved, and do it silently.
    expect(admit({ used: 18, incoming: 5, cap: 20 })).toEqual({
      accepted: false,
      reason: "cap_exceeded",
    });
  });

  it("distinguishes a file that will never fit from a queue that is full", () => {
    // They need opposite advice: "upload what is waiting" versus "this file
    // cannot be queued whatever you clear".
    expect(admit({ used: 0, incoming: 50, cap: 20 })).toEqual({
      accepted: false,
      reason: "larger_than_cap",
    });
  });

  it("reports larger_than_cap even when the queue is also full", () => {
    expect(admit({ used: 19, incoming: 50, cap: 20 }).accepted).toBe(false);
    expect(admit({ used: 19, incoming: 50, cap: 20 })).toEqual({
      accepted: false,
      reason: "larger_than_cap",
    });
  });
});

describe("backoffMs", () => {
  const noJitter = () => 0.5;

  it("grows exponentially from five seconds", () => {
    expect(backoffMs(1, noJitter)).toBe(5_000);
    expect(backoffMs(2, noJitter)).toBe(10_000);
    expect(backoffMs(3, noJitter)).toBe(20_000);
    expect(backoffMs(4, noJitter)).toBe(40_000);
  });

  it("stops growing at fifteen minutes", () => {
    expect(backoffMs(50, noJitter)).toBe(15 * 60 * 1_000);
    expect(backoffMs(500, noJitter)).toBe(15 * 60 * 1_000);
  });

  it("spreads attempts either side, so reconnecting phones do not sync up", () => {
    expect(backoffMs(1, () => 0)).toBe(4_000);
    expect(backoffMs(1, () => 1)).toBe(6_000);
  });

  it("never returns a negative or zero delay", () => {
    for (let attempts = 0; attempts < 20; attempts += 1) {
      expect(backoffMs(attempts, () => 0)).toBeGreaterThan(0);
    }
  });
});

describe("due", () => {
  const now = 1_000_000;

  it("takes queued records with no wait", () => {
    expect(due([record({ id: "a" })], now).map((r) => r.id)).toEqual(["a"]);
  });

  it("leaves a record whose backoff has not elapsed", () => {
    expect(due([record({ id: "a", nextAttemptAt: now + 1 })], now)).toEqual([]);
  });

  it("takes a record whose backoff has elapsed", () => {
    expect(due([record({ id: "a", nextAttemptAt: now })], now).map((r) => r.id)).toEqual(["a"]);
  });

  it("skips blocked records — retrying them is a person's decision", () => {
    expect(due([record({ id: "a", state: "blocked" })], now)).toEqual([]);
  });

  it("skips a record already in flight", () => {
    expect(due([record({ id: "a", state: "uploading" })], now)).toEqual([]);
  });

  it("orders oldest capture first, so a batch numbers as the pile was stacked", () => {
    // capture_page allocates position as max + 1 (ADR-031). Sending
    // newest-first would number a shoebox backwards against the paper.
    const records = [
      record({ id: "third", capturedAt: "2026-09-06T10:00:03.000Z" }),
      record({ id: "first", capturedAt: "2026-09-06T10:00:01.000Z" }),
      record({ id: "second", capturedAt: "2026-09-06T10:00:02.000Z" }),
    ];

    expect(due(records, now).map((r) => r.id)).toEqual(["first", "second", "third"]);
  });
});

describe("nextDueAt", () => {
  it("is null when nothing is waiting out a backoff", () => {
    expect(nextDueAt([record({ id: "a" })])).toBeNull();
  });

  it("is the earliest wait, so one timer covers the whole queue", () => {
    const records = [
      record({ id: "a", nextAttemptAt: 500 }),
      record({ id: "b", nextAttemptAt: 200 }),
      record({ id: "c", nextAttemptAt: 900 }),
    ];

    expect(nextDueAt(records)).toBe(200);
  });

  it("ignores blocked records, which no timer will ever make due", () => {
    expect(nextDueAt([record({ id: "a", state: "blocked", nextAttemptAt: 5 })])).toBeNull();
  });
});

describe("usedBytes", () => {
  it("counts blocked records against the cap", () => {
    // They still occupy the device, and they are still the only copy of a
    // photograph. Excluding them would let a queue of refusals grow unbounded.
    const records = [
      record({ id: "a", bytes: 10 }),
      record({ id: "b", state: "blocked", bytes: 7 }),
    ];

    expect(usedBytes(records)).toBe(17);
  });

  it("is zero for an empty queue", () => {
    expect(usedBytes([])).toBe(0);
  });
});
