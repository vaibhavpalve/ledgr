/**
 * The queue itself — FR-EXP-001f, MOB-003, MOB-009.
 *
 * Holds captures that have not reached the server, encrypted, capped, and
 * visible. It does not upload: `QueueUploader` does that, over this. The split
 * is what lets a capture screen depend on the queue without dragging a
 * transport and a connectivity listener behind it.
 *
 * --- Nothing reaches the store unsealed ---
 *
 * `enqueue` is the only way in and it seals before it writes. There is no
 * `putPlain`, no debug path and no "encrypt later" flag, because MOB-009 is
 * not a property of the happy path — it is a property of every path, and the
 * cheapest way to have it is to leave no other one.
 *
 * --- Purge destroys the key first ---
 *
 * See `purge`. The ordering is the whole design.
 */

import {
  DEFAULT_CAP_BYTES,
  type BlockedReason,
  type Bytes,
  type EnqueueResult,
  type NewCapture,
  type PurgeReason,
  type QueueSnapshot,
  type SealedPayload,
  type StoredCapture,
} from "./model";
import { frame, unframe } from "./envelope";
import { admit, countBy, laterPagesOf, usedBytes, view } from "./policy";
import type { Clock, PurgeEvent, QueueCipher, QueueStore } from "./ports";

export interface CaptureQueueOptions {
  store: QueueStore;
  cipher: QueueCipher;
  clock: Clock;
  /** Record ids and NFR-032 idempotency keys. `crypto.randomUUID` on both platforms. */
  newId: () => string;
  capBytes?: number;
  /** Told about every purge, so a composition root can log or report one. */
  onPurge?: (event: PurgeEvent) => void;
}

export class CaptureQueue {
  private readonly options: Required<Omit<CaptureQueueOptions, "onPurge">> &
    Pick<CaptureQueueOptions, "onPurge">;
  private readonly listeners = new Set<(snapshot: QueueSnapshot) => void>();
  private delivered = 0;
  private offline = false;

  constructor(options: CaptureQueueOptions) {
    this.options = { capBytes: DEFAULT_CAP_BYTES, ...options };
  }

  get capBytes(): number {
    return this.options.capBytes;
  }

  /**
   * FR-EXP-001f's "queued encrypted on-device".
   *
   * Called by the capture screen whether or not there is a connection. There
   * is no online fast path that skips the queue: a capture that went straight
   * out when the network happened to be up would be a second delivery path,
   * with its own retry story and its own bugs, and the one exercised least
   * often would be the one that runs when the tunnel starts. Everything is
   * queued; the uploader drains it, immediately when it can.
   */
  async enqueue(capture: NewCapture): Promise<EnqueueResult> {
    const records = await this.options.store.all();
    const id = this.options.newId();

    const payload: SealedPayload = {
      organizationId: capture.organizationId,
      administrationId: capture.administrationId,
      fiscalYearId: capture.fiscalYearId,
      userId: capture.userId,
      sessionId: capture.sessionId,
      receiptRef: capture.receiptRef,
      pageIndex: capture.pageIndex,
      source: capture.source,
      filename: capture.filename,
      contentType: capture.contentType,
      idempotencyKey: this.options.newId(),
      capturedAt: new Date(this.options.clock.now()).toISOString(),
    };

    const sealed = await this.options.cipher.seal(id, frame(payload, capture.image));
    const bytes = sealed.ciphertext.length;

    // Admission is decided on the SEALED size, because that is what the device
    // actually spends. Sealing before deciding costs one encryption on a
    // capture that is about to be refused, and buys a cap that means what it
    // says rather than one that is 16 bytes per record optimistic.
    const decision = admit({
      used: usedBytes(records),
      incoming: bytes,
      cap: this.options.capBytes,
    });
    if (!decision.accepted) {
      return { accepted: false, reason: decision.reason, capBytes: this.options.capBytes };
    }

    await this.options.store.put({
      id,
      state: "queued",
      receiptRef: capture.receiptRef,
      pageIndex: capture.pageIndex,
      // Page 0 creates the receipt, so it never needs one; a later page waits
      // for `resolveReceipt` before `due` will offer it.
      resolvedItemId: null,
      bytes,
      capturedAt: payload.capturedAt,
      attempts: 0,
      nextAttemptAt: null,
      lastAttemptAt: null,
      blockedReason: null,
      sealed,
    });
    await this.notify();
    return { accepted: true, id };
  }

  /** MOB-003's "visible queue state", and it needs no key. */
  async snapshot(): Promise<QueueSnapshot> {
    const records = await this.options.store.all();
    return {
      items: records
        .slice()
        .sort((a, b) => a.capturedAt.localeCompare(b.capturedAt))
        .map(view),
      queued: countBy(records, "queued"),
      uploading: countBy(records, "uploading"),
      blocked: countBy(records, "blocked"),
      delivered: this.delivered,
      usedBytes: usedBytes(records),
      capBytes: this.options.capBytes,
      offline: this.offline,
    };
  }

  subscribe(listener: (snapshot: QueueSnapshot) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * MOB-009: "purged on logout, role change or remote wipe."
   *
   * The KEY GOES FIRST. That ordering is the reason this is one method rather
   * than a call to `store.clear()` at three call sites.
   *
   * Deleting records is a loop over a database that can be interrupted — the
   * tab closes, the process is killed, the OS reclaims the app mid-wipe — and
   * an interrupted delete leaves readable ciphertext behind. Destroying the
   * one key first makes every remaining record permanently unreadable in a
   * single operation, so the loop afterwards is housekeeping rather than the
   * security boundary. If it never finishes, what survives is noise.
   *
   * The honest cost: this DESTROYS captures that never uploaded. A person
   * signing out with four receipts waiting loses four receipts. That is what
   * the requirement asks for, and the mitigation is to say so first — the
   * queue display carries the warning, and `pending()` is what a sign-out flow
   * checks before it offers the button.
   */
  async purge(reason: PurgeReason): Promise<PurgeEvent> {
    const discarded = (await this.options.store.all()).length;
    await this.options.cipher.destroyKey();
    await this.options.store.clear();
    this.delivered = 0;
    const event: PurgeEvent = { reason, discarded };
    this.options.onPurge?.(event);
    await this.notify();
    return event;
  }

  /**
   * How many captures a purge would destroy.
   *
   * Blocked records count. They are still the only copy of a photograph, and a
   * sign-out warning that quietly excluded them would understate what is about
   * to be lost.
   */
  async pending(): Promise<number> {
    return (await this.options.store.all()).length;
  }

  /**
   * A capture the person has chosen to drop — a retake, or a blocked one.
   *
   * Dropping a receipt's FIRST page drops the whole receipt. The alternative
   * is pages 2 and 3 of an invoice with no page 1 to join: they could never be
   * sent, and promoting one of them to first page would submit a claim
   * evidenced by the middle of a document. Discarding a later page leaves the
   * rest of the receipt alone, which is the retake case.
   */
  async discard(id: string): Promise<void> {
    const records = await this.options.store.all();
    const going = records.find((record) => record.id === id);
    await this.options.store.remove(id);

    if (going !== undefined && going.pageIndex === 0) {
      for (const sibling of laterPagesOf(records, going.receiptRef)) {
        await this.options.store.remove(sibling.id);
      }
    }
    await this.notify();
  }

  // -- used by QueueUploader ------------------------------------------------

  async records(): Promise<readonly StoredCapture[]> {
    return this.options.store.all();
  }

  /**
   * The decrypted capture, for an attempt about to be made.
   *
   * Nothing else in the package calls this, and nothing outside it should: it
   * is the one operation that needs the key and produces plaintext, and
   * keeping it to a single caller is what makes "the image is only ever in
   * memory during an upload" a checkable claim rather than a hope.
   */
  async open(record: StoredCapture): Promise<{ payload: SealedPayload; image: Bytes }> {
    return unframe(await this.options.cipher.open(record.id, record.sealed));
  }

  /**
   * Returns the record as it now stands, attempt count included, so the
   * caller marks the OUTCOME against the same version it wrote. Handing back
   * the updated record rather than letting the uploader keep its own copy is
   * what stops a backoff being computed from a stale attempt count — the bug
   * whose symptom is a queue that retries every five seconds forever.
   */
  async markUploading(record: StoredCapture): Promise<StoredCapture> {
    const updated: StoredCapture = {
      ...record,
      state: "uploading",
      attempts: record.attempts + 1,
      lastAttemptAt: this.options.clock.now(),
    };
    await this.options.store.put(updated);
    await this.notify();
    return updated;
  }

  async markDelivered(record: StoredCapture): Promise<void> {
    await this.options.store.remove(record.id);
    this.delivered += 1;
    await this.notify();
  }

  /** A retryable failure: still queued, not before `nextAttemptAt`. */
  async markWaiting(record: StoredCapture, nextAttemptAt: number): Promise<void> {
    await this.options.store.put({ ...record, state: "queued", nextAttemptAt });
    await this.notify();
  }

  /**
   * The receipt's first page landed; here is the server's id for it.
   *
   * Written onto every page of that receipt still in the queue, which is what
   * releases them: until this runs, `policy.due` will not offer a page whose
   * `pageIndex` is above zero, because sending one with no item would open a
   * second receipt for half an invoice.
   */
  async resolveReceipt(receiptRef: string, itemId: string): Promise<void> {
    const waiting = laterPagesOf(await this.options.store.all(), receiptRef);
    for (const record of waiting) {
      if (record.resolvedItemId !== null) continue;
      // A sibling blocked only because the first page was refused is released
      // here: the first page has now arrived, so the reason it carried has
      // stopped being true. Any other refusal was about that page itself and
      // stands.
      const released = record.blockedReason === "first_page_blocked";
      await this.options.store.put({
        ...record,
        resolvedItemId: itemId,
        state: released ? "queued" : record.state,
        blockedReason: released ? null : record.blockedReason,
        nextAttemptAt: released ? null : record.nextAttemptAt,
      });
    }
    await this.notify();
  }

  /**
   * Blocks this page, and — when it is the page that would have created the
   * receipt — every other page of that receipt with it.
   *
   * The cascade is what stops a queue holding pages 2 and 3 of an invoice
   * whose page 1 was refused. They can never resolve, `due` will never offer
   * them, and without this they would sit there forever with no reason shown.
   * `first_page_blocked` says what actually happened, which is a different
   * sentence from the one page 1 got.
   */
  async markBlocked(record: StoredCapture, reason: BlockedReason): Promise<void> {
    await this.options.store.put({
      ...record,
      state: "blocked",
      nextAttemptAt: null,
      blockedReason: reason,
    });

    if (record.pageIndex === 0) {
      const stranded = laterPagesOf(await this.options.store.all(), record.receiptRef);
      for (const sibling of stranded) {
        if (sibling.state === "blocked") continue;
        await this.options.store.put({
          ...sibling,
          state: "blocked",
          nextAttemptAt: null,
          blockedReason: "first_page_blocked",
        });
      }
    }
    await this.notify();
  }

  /**
   * A blocked capture the person has asked to try again.
   *
   * Offered because `blocked` includes refusals that a change elsewhere can
   * fix — a permission granted, a fiscal year opened — and the alternative to
   * a retry button is re-photographing a receipt that may be in a bin.
   */
  async unblock(id: string): Promise<void> {
    const record = (await this.options.store.all()).find((candidate) => candidate.id === id);
    if (record === undefined || record.state !== "blocked") return;
    await this.options.store.put({
      ...record,
      state: "queued",
      attempts: 0,
      nextAttemptAt: null,
      blockedReason: null,
    });
    await this.notify();
  }

  setOffline(offline: boolean): void {
    this.offline = offline;
    void this.notify();
  }

  private async notify(): Promise<void> {
    if (this.listeners.size === 0) return;
    const snapshot = await this.snapshot();
    for (const listener of this.listeners) listener(snapshot);
  }
}
