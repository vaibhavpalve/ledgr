import { describe, expect, it } from "vitest";

import {
  FakeCipher,
  MemoryStore,
  TestClock,
  TestConnectivity,
  TestScheduler,
  TestTransport,
  testIds,
} from "./doubles";
import type { NewCapture, TransportResponse } from "./index";
import { CaptureQueue } from "./queue";
import { QueueUploader } from "./uploader";

const capture: NewCapture = {
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
  image: new Uint8Array([1, 2, 3, 4]),
};

function build(responses: TransportResponse[] = [], { online = true } = {}) {
  const clock = new TestClock();
  const scheduler = new TestScheduler(clock);
  const connectivity = new TestConnectivity(online);
  const transport = new TestTransport(responses);
  const queue = new CaptureQueue({
    store: new MemoryStore(),
    cipher: new FakeCipher(),
    clock,
    newId: testIds(),
  });
  const uploader = new QueueUploader({
    queue,
    transport,
    connectivity,
    clock,
    scheduler,
    // Pinned, so a backoff is a number a test can name.
    random: () => 0.5,
  });
  return { clock, scheduler, connectivity, transport, queue, uploader };
}

const ok: TransportResponse = { kind: "response", status: 201, reason: null, itemId: "item-1" };
const offline: TransportResponse = { kind: "network_error" };

describe("draining when connectivity returns — FR-EXP-001f, MOB-003", () => {
  it("sends nothing while the device is offline", async () => {
    const { transport, queue, uploader } = build([], { online: false });
    await queue.enqueue(capture);

    uploader.start();
    await uploader.drain();

    expect(transport.sent).toEqual([]);
    expect((await queue.snapshot()).queued).toBe(1);
  });

  it("uploads automatically when connectivity returns, with no user action", async () => {
    // The sentence FR-EXP-001f is made of: photograph in a tunnel, walk out,
    // the receipt is on the server without anybody pressing anything.
    const { connectivity, transport, queue, uploader } = build([ok], { online: false });
    await queue.enqueue(capture);
    uploader.start();

    await connectivity.goOnline();

    expect(transport.sent).toHaveLength(1);
    expect((await queue.snapshot()).items).toEqual([]);
  });

  it("shows the queue as offline while it is, and not after", async () => {
    const { connectivity, queue, uploader } = build([ok], { online: false });
    await queue.enqueue(capture);

    uploader.start();
    expect((await queue.snapshot()).offline).toBe(true);

    await connectivity.goOnline();
    expect((await queue.snapshot()).offline).toBe(false);
  });

  it("removes a delivered capture and counts it", async () => {
    const { queue, uploader } = build([ok]);
    await queue.enqueue(capture);

    await uploader.drain();

    const snapshot = await queue.snapshot();
    expect(snapshot.items).toEqual([]);
    expect(snapshot.delivered).toBe(1);
    expect(snapshot.usedBytes).toBe(0);
  });

  it("sends oldest capture first", async () => {
    // capture_page allocates a receipt's position as max + 1 (ADR-031), so
    // the send order is the order a batch is numbered in — and the pile of
    // paper somebody checks it against is in the order it was photographed.
    const { clock, transport, queue, uploader } = build([ok, ok]);
    await queue.enqueue({ ...capture, filename: "first.jpg" });
    clock.advance(1_000);
    await queue.enqueue({ ...capture, filename: "second.jpg" });

    await uploader.drain();

    expect(transport.sent.map((request) => request.path)).toEqual([
      expect.stringContaining("filename=first.jpg"),
      expect.stringContaining("filename=second.jpg"),
    ]);
  });
});

describe("uploads that used to stall", () => {
  it("uploads a capture made while the app is already running, with no other wake-up", async () => {
    // Nothing else fires here: not the online event, not a timer, not a call to
    // drain(). Before, the capture sat `queued` until the next reload.
    const { transport, queue, uploader } = build([ok]);
    uploader.start();
    await new Promise((resolve) => setTimeout(resolve, 0));

    await queue.enqueue(capture);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(transport.sent).toHaveLength(1);
    expect((await queue.snapshot()).items).toEqual([]);
  });

  it("stops listening for captures once stopped", async () => {
    const { transport, queue, uploader } = build([ok]);
    uploader.start();
    uploader.stop();

    await queue.enqueue(capture);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(transport.sent).toEqual([]);
  });

  it("re-offers a capture left `uploading` by an attempt that was cut off", async () => {
    // A reload mid-request leaves the record `uploading`, which `due` never
    // offered - so it showed UPLOADING for ever and nothing sent it.
    const { transport, queue, uploader } = build([ok]);
    await queue.enqueue(capture);
    const [record] = await queue.records();
    await queue.markUploading(record!);
    expect((await queue.snapshot()).uploading).toBe(1);

    await uploader.drain();

    expect(transport.sent).toHaveLength(1);
    expect((await queue.snapshot()).items).toEqual([]);
  });

  it("keeps draining when one attempt throws instead of answering", async () => {
    const { clock, queue, uploader } = build();
    const sent: string[] = [];
    let first = true;
    const flaky = {
      async send(request: { path: string }): Promise<TransportResponse> {
        sent.push(request.path);
        if (first) {
          first = false;
          throw new Error("body could not be read");
        }
        return ok;
      },
    };
    const resilient = new QueueUploader({
      queue,
      transport: flaky,
      connectivity: new TestConnectivity(true),
      clock,
      scheduler: new TestScheduler(clock),
      random: () => 0.5,
    });
    await queue.enqueue({ ...capture, receiptRef: "a", filename: "a.pdf" });
    clock.advance(1_000);
    await queue.enqueue({ ...capture, receiptRef: "b", filename: "b.pdf" });

    await resilient.drain();

    // The throwing one is queued again with its attempt counted; the one behind
    // it was still sent rather than abandoned.
    expect(sent).toHaveLength(2);
    const [left] = (await queue.snapshot()).items;
    expect(left?.state).toBe("queued");
    expect(left?.attempts).toBe(1);
  });
});

describe("retrying", () => {
  it("keeps a capture queued after a network error and waits before trying again", async () => {
    const { clock, queue, uploader } = build([offline]);
    await queue.enqueue(capture);

    await uploader.drain();

    const [item] = (await queue.snapshot()).items;
    expect(item?.state).toBe("queued");
    expect(item?.attempts).toBe(1);
    expect(item?.nextAttemptAt).toBe(clock.now() + 5_000);
  });

  it("does not re-send before the backoff has elapsed", async () => {
    const { transport, queue, uploader } = build([offline, ok]);
    await queue.enqueue(capture);

    await uploader.drain();
    await uploader.drain();

    expect(transport.sent).toHaveLength(1);
  });

  it("sends again once the backoff timer fires", async () => {
    // The fallback for a connection that came back without the platform
    // saying so — a captive portal agreed to, a VPN that finished.
    const { scheduler, transport, queue, uploader } = build([offline, ok]);
    await queue.enqueue(capture);
    await uploader.drain();

    await scheduler.runDueAfter(5_000);

    expect(transport.sent).toHaveLength(2);
    expect((await queue.snapshot()).items).toEqual([]);
  });

  it("backs off further on each consecutive failure", async () => {
    const { clock, queue, uploader } = build([offline, offline]);
    await queue.enqueue(capture);

    await uploader.drain();
    clock.advance(5_000);
    await uploader.drain();

    const [item] = (await queue.snapshot()).items;
    expect(item?.attempts).toBe(2);
    expect(item?.nextAttemptAt).toBe(clock.now() + 10_000);
  });

  it("gives up on the rest of the batch when the connection drops", async () => {
    // Offering thirty more receipts to a dead network fails thirty times,
    // spends thirty records' worth of backoff, and moves nothing.
    const { transport, queue, uploader } = build([offline, ok, ok]);
    await queue.enqueue(capture);
    await queue.enqueue(capture);
    await queue.enqueue(capture);

    await uploader.drain();

    expect(transport.sent).toHaveLength(1);
    expect((await queue.snapshot()).offline).toBe(true);
  });

  it("carries on through a server error, which is about one request", async () => {
    const serverError: TransportResponse = {
      kind: "response",
      status: 503,
      reason: null,
      itemId: null,
    };
    const { transport, queue, uploader } = build([serverError, ok]);
    await queue.enqueue(capture);
    await queue.enqueue(capture);

    await uploader.drain();

    expect(transport.sent).toHaveLength(2);
    expect((await queue.snapshot()).queued).toBe(1);
  });

  it("retries with the same idempotency key — NFR-032", async () => {
    // The property the whole retry story rests on. A response lost after the
    // server committed must not become a second expense.
    const { scheduler, transport, queue, uploader } = build([offline, ok]);
    await queue.enqueue(capture);
    await uploader.drain();

    await scheduler.runDueAfter(5_000);

    expect(transport.sent).toHaveLength(2);
    expect(transport.sent[0]?.headers["Idempotency-Key"]).toBe(
      transport.sent[1]?.headers["Idempotency-Key"],
    );
  });
});

describe("multi-page receipts captured offline — FR-EXP-001, ADR-031 §3", () => {
  const page = (receiptRef: string, pageIndex: number) => ({ ...capture, receiptRef, pageIndex });

  it("opens the receipt with page 0 and joins every later page to it", async () => {
    // The whole point. A three-page invoice photographed in a tunnel is ONE
    // expense, and the item id that makes it one does not exist until page 0
    // has been somewhere.
    const created: TransportResponse = {
      kind: "response",
      status: 201,
      reason: null,
      itemId: "item-42",
    };
    const { clock, transport, queue, uploader } = build([created, ok, ok]);
    await queue.enqueue(page("invoice", 0));
    clock.advance(1_000);
    await queue.enqueue(page("invoice", 1));
    clock.advance(1_000);
    await queue.enqueue(page("invoice", 2));

    await uploader.drain();

    expect(transport.sent).toHaveLength(3);
    expect(transport.sent[0]?.path).not.toContain("item=");
    expect(transport.sent[1]?.path).toContain("item=item-42");
    expect(transport.sent[2]?.path).toContain("item=item-42");
  });

  it("holds a later page back while page 0 is waiting out a backoff", async () => {
    // Sequential ordering alone does not cover this: page 0 is not due, so a
    // queue that only sorted by time would skip to page 1 and open a SECOND
    // receipt — one invoice claimed twice.
    const { clock, transport, queue, uploader } = build([offline]);
    await queue.enqueue(page("invoice", 0));
    clock.advance(1_000);
    await queue.enqueue(page("invoice", 1));

    await uploader.drain();

    expect(transport.sent).toHaveLength(1);
    expect(transport.sent[0]?.path).not.toContain("item=");
  });

  it("numbers receipts in capture order even when a page is held back", async () => {
    // A held-back page defers to the next receipt's first page, and that is
    // harmless: `position` is allocated per ITEM as max + 1 (ADR-031), and a
    // further page of an existing item allocates none. So what has to hold is
    // that the RECEIPT-OPENING requests keep their order — the order the pile
    // of paper is in — while the deferred page still joins its own receipt.
    const created: TransportResponse = {
      kind: "response",
      status: 201,
      reason: null,
      itemId: "item-7",
    };
    const { clock, transport, queue, uploader } = build([created, ok, ok]);
    await queue.enqueue({ ...page("invoice", 0), filename: "invoice-1.jpg" });
    clock.advance(1_000);
    await queue.enqueue({ ...page("invoice", 1), filename: "invoice-2.jpg" });
    clock.advance(1_000);
    await queue.enqueue({ ...page("lunch", 0), filename: "lunch.jpg" });

    await uploader.drain();

    const opening = transport.sent.filter((request) => !request.path.includes("item="));
    expect(opening.map((request) => request.path)).toEqual([
      expect.stringContaining("filename=invoice-1.jpg"),
      expect.stringContaining("filename=lunch.jpg"),
    ]);

    const joining = transport.sent.filter((request) => request.path.includes("item="));
    expect(joining).toHaveLength(1);
    expect(joining[0]?.path).toContain("filename=invoice-2.jpg");
    expect(joining[0]?.path).toContain("item=item-7");
  });

  it("blocks the rest of a receipt when its first page is refused", async () => {
    // Pages 2 and 3 can never resolve. Left queued they would sit forever with
    // nothing to explain themselves; `first_page_blocked` says what happened.
    const rejected: TransportResponse = {
      kind: "response",
      status: 415,
      reason: "unsupported_document_type",
      itemId: null,
    };
    const { clock, queue, uploader } = build([rejected]);
    await queue.enqueue(page("invoice", 0));
    clock.advance(1_000);
    await queue.enqueue(page("invoice", 1));

    await uploader.drain();

    const reasons = (await queue.snapshot()).items.map((item) => item.blockedReason);
    expect(reasons).toEqual(["unsupported_type", "first_page_blocked"]);
  });

  it("releases the rest of a receipt when a retried first page succeeds", async () => {
    const denied: TransportResponse = { kind: "response", status: 403, reason: null, itemId: null };
    const created: TransportResponse = {
      kind: "response",
      status: 201,
      reason: null,
      itemId: "item-5",
    };
    const { clock, transport, queue, uploader } = build([denied, created, ok]);
    await queue.enqueue(page("invoice", 0));
    clock.advance(1_000);
    await queue.enqueue(page("invoice", 1));
    await uploader.drain();
    const [first] = await queue.records();

    await queue.unblock(first!.id);
    await uploader.drain();

    expect(transport.sent).toHaveLength(3);
    expect(transport.sent[2]?.path).toContain("item=item-5");
    expect((await queue.snapshot()).items).toEqual([]);
  });

  it("leaves a delivered page's siblings waiting when the item id did not come back", async () => {
    // Better waiting than released: a sibling sent with no item would open a
    // second receipt, and waiting is recoverable by a retry of page 0.
    const noItem: TransportResponse = {
      kind: "response",
      status: 201,
      reason: null,
      itemId: null,
    };
    const { clock, transport, queue, uploader } = build([noItem]);
    await queue.enqueue(page("invoice", 0));
    clock.advance(1_000);
    await queue.enqueue(page("invoice", 1));

    await uploader.drain();

    expect(transport.sent).toHaveLength(1);
    expect((await queue.snapshot()).queued).toBe(1);
  });
});

describe("blocking", () => {
  it("stops retrying a format the archive will never accept", async () => {
    const rejected: TransportResponse = {
      kind: "response",
      status: 415,
      reason: "unsupported_document_type",
      itemId: null,
    };
    const { transport, queue, uploader } = build([rejected]);
    await queue.enqueue(capture);

    await uploader.drain();
    await uploader.drain();

    expect(transport.sent).toHaveLength(1);
    const [item] = (await queue.snapshot()).items;
    expect(item?.state).toBe("blocked");
    expect(item?.blockedReason).toBe("unsupported_type");
  });

  it("keeps a blocked capture rather than discarding it", async () => {
    const infected: TransportResponse = {
      kind: "response",
      status: 422,
      reason: "document_infected",
      itemId: null,
    };
    const { queue, uploader } = build([infected]);
    await queue.enqueue(capture);

    await uploader.drain();

    expect((await queue.snapshot()).blocked).toBe(1);
    expect(await queue.pending()).toBe(1);
  });

  it("sends a capture again after a person unblocks it", async () => {
    const denied: TransportResponse = { kind: "response", status: 403, reason: null, itemId: null };
    const { transport, queue, uploader } = build([denied, ok]);
    await queue.enqueue(capture);
    await uploader.drain();
    const [blocked] = await queue.records();

    await queue.unblock(blocked!.id);
    await uploader.drain();

    expect(transport.sent).toHaveLength(2);
    expect((await queue.snapshot()).items).toEqual([]);
  });
});

describe("tenant context", () => {
  it("uploads each capture to the administration it was taken for", async () => {
    // CLAUDE.md's first rule, client-side. The uploader is given no notion of
    // a "currently open" client, so a session that has since switched cannot
    // redirect a receipt.
    const { transport, clock, queue, uploader } = build([ok, ok]);
    await queue.enqueue({ ...capture, administrationId: "adm-A" });
    clock.advance(1_000);
    await queue.enqueue({ ...capture, administrationId: "adm-B", sessionId: "sess-2" });

    await uploader.drain();

    expect(transport.sent[0]?.path).toContain("/v1/administrations/adm-A/");
    expect(transport.sent[1]?.path).toContain("/v1/administrations/adm-B/");
  });

  it("carries the headers the composition root supplies at attempt time", async () => {
    const clock = new TestClock();
    const queue = new CaptureQueue({
      store: new MemoryStore(),
      cipher: new FakeCipher(),
      clock,
      newId: testIds(),
    });
    const transport = new TestTransport([ok]);
    let token = "first";
    const uploader = new QueueUploader({
      queue,
      transport,
      connectivity: new TestConnectivity(true),
      clock,
      scheduler: new TestScheduler(clock),
      headers: () => ({ Authorization: `Bearer ${token}`, "Accept-Language": "nl" }),
    });
    await queue.enqueue(capture);
    token = "refreshed";

    await uploader.drain();

    expect(transport.sent[0]?.headers["Authorization"]).toBe("Bearer refreshed");
    expect(transport.sent[0]?.headers["Accept-Language"]).toBe("nl");
  });
});

describe("one drain at a time", () => {
  it("collapses overlapping wake-ups into one pass and one follow-up", async () => {
    // An online event, a backoff timer and a retry tap arrive together all the
    // time. Concurrent passes would open and send the same record twice —
    // which NFR-032 makes safe, and which still doubles the upload on a
    // metered connection the queue exists to be careful with.
    const { transport, queue, uploader } = build([ok, ok, ok]);
    await queue.enqueue(capture);

    await Promise.all([uploader.drain(), uploader.drain(), uploader.drain()]);

    expect(transport.sent).toHaveLength(1);
  });

  it("stops listening and cancels its timer when stopped", async () => {
    const { scheduler, transport, connectivity, queue, uploader } = build([offline, ok]);
    await queue.enqueue(capture);
    uploader.start();
    await uploader.drain();

    uploader.stop();
    await connectivity.goOnline();
    await scheduler.runDueAfter(60_000);

    expect(transport.sent).toHaveLength(1);
  });
});

describe("when the store cannot be read", () => {
  it("does not leave an unhandled rejection behind when it starts", async () => {
    const { queue, uploader } = build();
    // No IndexedDB in some private-browsing modes; here, a store that refuses.
    queue.records = () => Promise.reject(new Error("indexedDB is not defined"));

    const unhandled: unknown[] = [];
    const listener = (reason: unknown) => unhandled.push(reason);
    process.on("unhandledRejection", listener);
    try {
      uploader.start();
      // Let the rejected drain settle and any unhandled-rejection event fire.
      await new Promise((resolve) => setTimeout(resolve, 20));
    } finally {
      process.off("unhandledRejection", listener);
    }

    expect(unhandled).toEqual([]);
  });

  it("still reports the failure to a caller that awaits drain() itself", async () => {
    const { queue, uploader } = build();
    queue.records = () => Promise.reject(new Error("store unavailable"));

    await expect(uploader.drain()).rejects.toThrow("store unavailable");
  });
});
