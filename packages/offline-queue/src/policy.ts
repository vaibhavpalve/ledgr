/**
 * Every decision the queue makes, as pure functions — MOB-003, MOB-009.
 *
 * The cap, the backoff and the choice of what to send next are here rather
 * than inside the queue object because they are the parts most worth testing
 * and least worth mocking a database for. `CaptureQueue` and `QueueUploader`
 * do I/O; this file decides.
 */

import type { QueueItemView, QueueState, RefusalReason, StoredCapture } from "./model";

export function usedBytes(records: readonly StoredCapture[]): number {
  return records.reduce((total, record) => total + record.bytes, 0);
}

export type Admission =
  { readonly accepted: true } | { readonly accepted: false; readonly reason: RefusalReason };

/**
 * Whether a capture of `incomingBytes` fits — MOB-009's "size-capped".
 *
 * When it does not fit, the answer is to REFUSE THE NEW CAPTURE, never to
 * evict a queued one. That is the whole decision, and it goes against the
 * usual instinct for a cache.
 *
 * A queued receipt is not a cached copy of something. On a phone with no
 * connectivity it is the ONLY copy: the photograph was taken by the app, it is
 * not in the camera roll, and the paper receipt may already be in a bin. Making
 * room by dropping the oldest one destroys a document its owner believes is
 * saved, silently, at the moment they are least able to notice — which is the
 * failure FR-DOC-003's completeness report exists to find months later.
 *
 * Refusing is loud and lands while the person is still holding the receipt.
 * They can free the queue by getting online, or by discarding something they
 * can see.
 *
 * `larger_than_cap` is separated from `cap_exceeded` because they need
 * opposite advice: one is "upload what is waiting", the other is "this file
 * will never fit, whatever you clear".
 */
export function admit({
  used,
  incoming,
  cap,
}: {
  used: number;
  incoming: number;
  cap: number;
}): Admission {
  if (incoming > cap) return { accepted: false, reason: "larger_than_cap" };
  if (used + incoming > cap) return { accepted: false, reason: "cap_exceeded" };
  return { accepted: true };
}

/** First retry after 5s. */
const BASE_BACKOFF_MS = 5_000;

/**
 * Ceiling on the wait between attempts.
 *
 * Fifteen minutes, not an hour: the common cause of a failed attempt here is a
 * connection that is coming back, and the `online` event usually beats the
 * timer anyway. This is the fallback for the cases the event misses — a
 * captive portal that starts working, a server that finishes deploying.
 */
const MAX_BACKOFF_MS = 15 * 60 * 1_000;

/** ±20%, so a fleet of phones reconnecting to one network does not sync up. */
const JITTER = 0.2;

/**
 * How long to wait after `attempts` consecutive retryable failures.
 *
 * There is deliberately no attempt limit. A retryable failure is a network
 * error, a 5xx or a 429 — all of which are conditions that end — and a queue
 * that gave up after ten tries would discard a receipt because a server was
 * down over a weekend. What a stuck item gets instead is visibility: the
 * attempt count and the next attempt time are in the snapshot, so a queue that
 * is not draining looks like one that is not draining.
 *
 * Refusals that will never succeed are not retried at all; they are `blocked`
 * (see classify in request.ts).
 *
 * `random` is injected rather than called, so the jitter is a value a test can
 * pin instead of a range it has to tolerate.
 */
export function backoffMs(attempts: number, random: () => number = Math.random): number {
  const exponential = BASE_BACKOFF_MS * 2 ** Math.max(0, attempts - 1);
  const capped = Math.min(exponential, MAX_BACKOFF_MS);
  const jitter = 1 + (random() * 2 - 1) * JITTER;
  return Math.round(capped * jitter);
}

/**
 * Records the uploader may attempt now, oldest capture first.
 *
 * Oldest first, and not by size or by what is most likely to succeed: the
 * order a person photographed a pile of receipts in is the order the pile was
 * in, and `capture_page` allocates its position as max + 1 (ADR-031). Sending
 * newest-first would number a batch backwards against the paper somebody is
 * checking it against.
 */
export function due(records: readonly StoredCapture[], now: number): readonly StoredCapture[] {
  return records
    .filter((record) => record.state === "queued")
    .filter((record) => record.nextAttemptAt === null || record.nextAttemptAt <= now)
    .filter(canOpenItsReceipt)
    .slice()
    .sort((a, b) => a.capturedAt.localeCompare(b.capturedAt));
}

/**
 * Whether this page knows which receipt to join.
 *
 * Page 0 always does — it is the one that CREATES the receipt. Any later page
 * needs the server's item id, which only arrives when page 0 is delivered.
 *
 * Holding the later page back is the whole point. Sending it with no item id
 * would open a SECOND receipt, and FR-EXP-001a would turn one three-page
 * invoice into three separate expenses — the failure ADR-031 named as the
 * costliest available, because it is silent and it multiplies a claim.
 *
 * Sequential oldest-first ordering means this is usually moot: page 0 goes
 * first and resolves before page 1 is reached. It is not moot when page 0 is
 * waiting out a backoff, or was refused — and those are exactly the cases where
 * skipping to page 1 would do the damage.
 */
export function canOpenItsReceipt(record: StoredCapture): boolean {
  return record.pageIndex === 0 || record.resolvedItemId !== null;
}

/**
 * The pages of one receipt that DEPEND on its first page — everything a
 * resolution releases, and everything a block or a discard of page 0 takes
 * with it.
 *
 * `pageIndex > 0` rather than "every record but this one": a receipt has
 * exactly one page 0, so anything else sharing the reference at index 0 would
 * be a different receipt that happens to collide, and cascading onto it would
 * block a receipt for something that never happened to it.
 */
export function laterPagesOf(
  records: readonly StoredCapture[],
  receiptRef: string,
): readonly StoredCapture[] {
  return records.filter((record) => record.receiptRef === receiptRef && record.pageIndex > 0);
}

/**
 * When the next waiting record becomes due, or null if none is waiting.
 *
 * Drives the uploader's fallback timer: one timer for the whole queue rather
 * than one per record, so a queue of forty receipts is one pending callback.
 */
export function nextDueAt(records: readonly StoredCapture[]): number | null {
  const waiting = records
    .filter((record) => record.state === "queued" && record.nextAttemptAt !== null)
    .filter(canOpenItsReceipt)
    .map((record) => record.nextAttemptAt as number);
  return waiting.length === 0 ? null : Math.min(...waiting);
}

export function countBy(records: readonly StoredCapture[], state: QueueState): number {
  return records.filter((record) => record.state === state).length;
}

/**
 * The cleartext view of a record — MOB-003's "visible queue state".
 *
 * Nothing here needs the key, which is what lets a locked device (MOB-008)
 * still show how much work is waiting.
 */
export function view(record: StoredCapture): QueueItemView {
  return {
    id: record.id,
    state: record.state,
    receiptRef: record.receiptRef,
    pageIndex: record.pageIndex,
    bytes: record.bytes,
    capturedAt: record.capturedAt,
    attempts: record.attempts,
    nextAttemptAt: record.nextAttemptAt,
    blockedReason: record.blockedReason,
  };
}
