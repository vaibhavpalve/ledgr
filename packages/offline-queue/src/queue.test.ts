import { beforeEach, describe, expect, it, vi } from "vitest";

import { FakeCipher, MemoryStore, TestClock, testIds } from "./doubles";
import { unframe } from "./envelope";
import type { NewCapture } from "./model";
import { CaptureQueue } from "./queue";

const capture: NewCapture = {
  organizationId: "org-1",
  administrationId: "adm-A",
  fiscalYearId: "fy-2026",
  userId: "user-1",
  sessionId: "sess-1",
  receiptRef: "receipt-1",
  pageIndex: 0,
  source: "camera",
  filename: "receipt.jpg",
  contentType: "image/jpeg",
  image: new Uint8Array([0xff, 0xd8, 0xff, 0xe0]),
};

function build(capBytes?: number) {
  const store = new MemoryStore();
  const cipher = new FakeCipher();
  const clock = new TestClock();
  const queue = new CaptureQueue({ store, cipher, clock, newId: testIds(), capBytes });
  return { store, cipher, clock, queue };
}

/**
 * What one sealed capture costs, measured rather than assumed.
 *
 * The envelope carries the metadata as well as the image, so a record is a few
 * hundred bytes larger than the photograph. A cap test written against a
 * guessed number would pass for the wrong reason the day the payload gains a
 * field.
 */
async function recordSize(): Promise<number> {
  const { store, queue } = build();
  await queue.enqueue(capture);
  const [record] = await store.all();
  return record!.bytes;
}

describe("enqueue — FR-EXP-001f", () => {
  it("accepts a capture and reports its id", async () => {
    const { queue } = build();

    const result = await queue.enqueue(capture);

    expect(result.accepted).toBe(true);
  });

  it("queues whether or not there is a connection", async () => {
    // There is deliberately no online fast path that skips the queue. A second
    // delivery path would have its own retry story, and the one exercised
    // least often would be the one that runs when the tunnel starts.
    const { queue } = build();

    expect((await queue.enqueue(capture)).accepted).toBe(true);
    expect((await queue.snapshot()).queued).toBe(1);
  });

  it("writes nothing to the store unsealed — MOB-009", async () => {
    const { store, queue } = build();

    await queue.enqueue(capture);
    const [record] = await store.all();

    // The image's own leading bytes must not be recoverable from what was
    // stored, and neither must the administration it belongs to.
    const stored = JSON.stringify([...(record?.sealed.ciphertext ?? [])]);
    expect(stored).not.toContain("255,216,255,224");
    expect(new TextDecoder().decode(record?.sealed.ciphertext)).not.toContain("adm-A");
  });

  it("keeps the tenant context inside the envelope, not beside it", async () => {
    const { store, cipher, queue } = build();

    await queue.enqueue(capture);
    const [record] = await store.all();
    const cleartextFields = Object.keys(record ?? {});

    expect(cleartextFields).not.toContain("administrationId");
    expect(cleartextFields).not.toContain("filename");

    const { payload } = unframe(await cipher.open(record!.id, record!.sealed));
    expect(payload.administrationId).toBe("adm-A");
    expect(payload.userId).toBe("user-1");
  });

  it("mints one idempotency key per capture and keeps it — NFR-032", async () => {
    const { store, cipher, queue } = build();

    await queue.enqueue(capture);
    await queue.enqueue(capture);
    const records = await store.all();
    const keys = await Promise.all(
      records.map(
        async (record) =>
          unframe(await cipher.open(record.id, record.sealed)).payload.idempotencyKey,
      ),
    );

    expect(new Set(keys).size).toBe(2);
  });

  it("stamps the capture time from the clock, not from the upload", async () => {
    const { clock, queue } = build();
    clock.advance(60_000);

    await queue.enqueue(capture);

    expect((await queue.snapshot()).items[0]?.capturedAt).toBe(new Date(clock.now()).toISOString());
  });
});

describe("the size cap — MOB-009", () => {
  it("refuses a capture that would exceed the cap, and says which cap", async () => {
    const cap = (await recordSize()) + 1;
    const { queue } = build(cap);

    await queue.enqueue(capture);
    const second = await queue.enqueue(capture);

    expect(second).toEqual({ accepted: false, reason: "cap_exceeded", capBytes: cap });
  });

  it("keeps the earlier capture rather than evicting it for the new one", async () => {
    // The decision the whole cap turns on: the queued receipt is the only copy
    // of a photograph somebody believes is saved.
    const { queue } = build((await recordSize()) + 1);

    const first = await queue.enqueue(capture);
    await queue.enqueue(capture);
    const snapshot = await queue.snapshot();

    expect(snapshot.items.map((item) => item.id)).toEqual([first.accepted ? first.id : "gone"]);
  });

  it("names a file that will never fit differently from a full queue", async () => {
    const cap = (await recordSize()) - 1;
    const { queue } = build(cap);

    expect(await queue.enqueue(capture)).toEqual({
      accepted: false,
      reason: "larger_than_cap",
      capBytes: cap,
    });
  });

  it("frees the budget when a capture is delivered", async () => {
    const { queue } = build((await recordSize()) + 1);

    await queue.enqueue(capture);
    const [record] = await queue.records();
    await queue.markDelivered(record!);

    expect((await queue.enqueue(capture)).accepted).toBe(true);
  });

  it("does not free the budget when a capture is blocked", async () => {
    // A blocked capture is still a photograph nobody else has. It keeps its
    // bytes, and clearing it is the person's decision, not the queue's.
    const { queue } = build((await recordSize()) + 1);

    await queue.enqueue(capture);
    const [record] = await queue.records();
    await queue.markBlocked(record!, "unsupported_type");

    expect((await queue.enqueue(capture)).accepted).toBe(false);
  });
});

describe("snapshot — MOB-003's visible queue state", () => {
  it("counts each state separately", async () => {
    const { queue } = build();

    await queue.enqueue(capture);
    await queue.enqueue(capture);
    const [first] = await queue.records();
    await queue.markBlocked(first!, "infected");

    const snapshot = await queue.snapshot();
    expect(snapshot.queued).toBe(1);
    expect(snapshot.blocked).toBe(1);
    expect(snapshot.usedBytes).toBe(2 * (await recordSize()));
  });

  it("needs no key, so a locked device can still show what is waiting", async () => {
    // MOB-008's auto-lock must not make the queue invisible: "4 receipts
    // waiting" is the answer somebody needs before they unlock, not after.
    const { cipher, queue } = build();
    await queue.enqueue(capture);

    await cipher.destroyKey();

    expect((await queue.snapshot()).queued).toBe(1);
  });

  it("reports deliveries so a drain shows progress", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    const [record] = await queue.records();

    await queue.markDelivered(record!);

    const snapshot = await queue.snapshot();
    expect(snapshot.delivered).toBe(1);
    expect(snapshot.items).toEqual([]);
  });

  it("orders items oldest first", async () => {
    const { clock, queue } = build();
    const first = await queue.enqueue(capture);
    clock.advance(1_000);
    const second = await queue.enqueue(capture);

    const ids = (await queue.snapshot()).items.map((item) => item.id);

    expect(ids).toEqual([first.accepted ? first.id : "?", second.accepted ? second.id : "?"]);
  });

  it("tells subscribers on every change", async () => {
    const { queue } = build();
    const listener = vi.fn();
    queue.subscribe(listener);

    await queue.enqueue(capture);

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener.mock.calls[0]?.[0].queued).toBe(1);
  });

  it("stops telling a subscriber that has unsubscribed", async () => {
    const { queue } = build();
    const listener = vi.fn();
    queue.subscribe(listener)();

    await queue.enqueue(capture);

    expect(listener).not.toHaveBeenCalled();
  });
});

describe("purge — MOB-009's logout, role change and remote wipe", () => {
  let built: ReturnType<typeof build>;

  beforeEach(async () => {
    built = build();
    await built.queue.enqueue(capture);
    await built.queue.enqueue(capture);
  });

  it.each(["logout", "role_change", "remote_wipe"] as const)(
    "empties the queue on %s",
    async (reason) => {
      const event = await built.queue.purge(reason);

      expect(event).toEqual({ reason, discarded: 2 });
      expect(await built.store.all()).toEqual([]);
    },
  );

  it("destroys the key BEFORE the records", async () => {
    // The ordering is the design. Deleting records is a loop that can be
    // interrupted — the tab closes, the process is killed — and an interrupted
    // delete leaves readable ciphertext. Destroying the one key first makes
    // whatever survives unreadable, so the loop is housekeeping rather than
    // the security boundary.
    const order: string[] = [];
    const cipher = new FakeCipher();
    const store = new MemoryStore();
    vi.spyOn(cipher, "destroyKey").mockImplementation(async () => {
      order.push("key");
    });
    vi.spyOn(store, "clear").mockImplementation(async () => {
      order.push("records");
    });
    const queue = new CaptureQueue({ store, cipher, clock: new TestClock(), newId: testIds() });

    await queue.purge("logout");

    expect(order).toEqual(["key", "records"]);
  });

  it("leaves nothing readable even if the record delete never happens", async () => {
    const { cipher, store, queue } = built;
    vi.spyOn(store, "clear").mockRejectedValueOnce(new Error("process killed"));

    await expect(queue.purge("remote_wipe")).rejects.toThrow("process killed");

    const [survivor] = await store.all();
    await expect(cipher.open(survivor!.id, survivor!.sealed)).rejects.toThrow();
  });

  it("counts blocked captures in what a purge would destroy", async () => {
    // A sign-out warning that excluded them would understate the loss.
    const [first] = await built.queue.records();
    await built.queue.markBlocked(first!, "not_permitted");

    expect(await built.queue.pending()).toBe(2);
  });

  it("resets the delivered counter, which belongs to the session that ended", async () => {
    const [record] = await built.queue.records();
    await built.queue.markDelivered(record!);

    await built.queue.purge("logout");

    expect((await built.queue.snapshot()).delivered).toBe(0);
  });
});

describe("attempts and blocking", () => {
  it("raises the attempt count when an upload starts", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    const [record] = await queue.records();

    const attempted = await queue.markUploading(record!);

    expect(attempted.attempts).toBe(1);
    expect(attempted.state).toBe("uploading");
  });

  it("returns the updated record, so a backoff is not computed from a stale count", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    const [record] = await queue.records();

    const first = await queue.markUploading(record!);
    const second = await queue.markUploading(first);

    expect(second.attempts).toBe(2);
  });

  it("puts a blocked capture in front of a person with a reason", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    const [record] = await queue.records();

    await queue.markBlocked(record!, "unsupported_type");

    const [item] = (await queue.snapshot()).items;
    expect(item?.state).toBe("blocked");
    expect(item?.blockedReason).toBe("unsupported_type");
  });

  it("lets a person retry a blocked capture, clearing the reason and the count", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    const [record] = await queue.records();
    await queue.markUploading(record!);
    await queue.markBlocked(record!, "not_permitted");

    await queue.unblock(record!.id);

    const [item] = (await queue.snapshot()).items;
    expect(item?.state).toBe("queued");
    expect(item?.blockedReason).toBeNull();
    expect(item?.attempts).toBe(0);
  });

  it("ignores unblock for a record that is not blocked", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    const [record] = await queue.records();

    await queue.unblock(record!.id);
    await queue.unblock("no-such-record");

    expect((await queue.snapshot()).queued).toBe(1);
  });

  it("discards a whole receipt when its first page is dropped", async () => {
    // Pages 2 and 3 with no page 1 could never be sent, and promoting one to
    // first page would submit a claim evidenced by the middle of a document.
    const { queue } = build();
    const first = await queue.enqueue({ ...capture, receiptRef: "invoice", pageIndex: 0 });
    await queue.enqueue({ ...capture, receiptRef: "invoice", pageIndex: 1 });
    await queue.enqueue({ ...capture, receiptRef: "lunch", pageIndex: 0 });

    await queue.discard(first.accepted ? first.id : "");

    const refs = (await queue.snapshot()).items.map((item) => item.receiptRef);
    expect(refs).toEqual(["lunch"]);
  });

  it("keeps the rest of a receipt when a later page is dropped — the retake case", async () => {
    const { queue } = build();
    await queue.enqueue({ ...capture, receiptRef: "invoice", pageIndex: 0 });
    const second = await queue.enqueue({ ...capture, receiptRef: "invoice", pageIndex: 1 });

    await queue.discard(second.accepted ? second.id : "");

    expect((await queue.snapshot()).items.map((item) => item.pageIndex)).toEqual([0]);
  });

  it("releases a receipt's waiting pages once its first page has an item id", async () => {
    const { queue } = build();
    await queue.enqueue({ ...capture, receiptRef: "invoice", pageIndex: 0 });
    await queue.enqueue({ ...capture, receiptRef: "invoice", pageIndex: 1 });

    await queue.resolveReceipt("invoice", "item-3");

    const [, second] = await queue.records();
    expect(second?.resolvedItemId).toBe("item-3");
  });

  it("discards a capture the person chose to drop", async () => {
    const { queue } = build();
    const result = await queue.enqueue(capture);

    await queue.discard(result.accepted ? result.id : "");

    expect((await queue.snapshot()).items).toEqual([]);
  });
});
