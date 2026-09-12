/**
 * "Uploads automatically when connectivity returns" — FR-EXP-001f, MOB-003.
 *
 * Three things wake a drain, and it needs all three:
 *
 *   the online event   the fast path. Usually first, and usually right.
 *   a backoff timer    for a record waiting out a failure.
 *   an explicit call   `drain()`, from a retry button or an app resume.
 *
 * `navigator.onLine` (and NetInfo) report that an interface is up, not that
 * the API is reachable: a captive portal, a hotel wifi that has not been
 * agreed to, and a VPN mid-handshake all satisfy it. So the event is a hint
 * that starts an attempt, never a fact the queue trusts — a network error
 * while it says "online" is an ordinary retryable failure, and the timer is
 * what covers a connection that came back without the event firing.
 *
 * --- One drain at a time ---
 *
 * The three wake-ups overlap constantly: an online event lands while a backoff
 * timer fires while the person taps retry. `drain` is single-flight and
 * remembers that it was asked again, so concurrent wake-ups collapse into one
 * pass followed by at most one more. Without it the same record is opened and
 * sent twice at once — which NFR-032's idempotency key makes SAFE, but safe is
 * not the same as sane: it doubles the upload on a metered connection the
 * queue exists to be careful with.
 */

import { backoffMs, due, nextDueAt } from "./policy";
import type { CaptureQueue } from "./queue";
import type { Cancel, Clock, Connectivity, Scheduler, Transport } from "./ports";
import { buildRequest, classify } from "./request";

export interface QueueUploaderOptions {
  queue: CaptureQueue;
  transport: Transport;
  connectivity: Connectivity;
  clock: Clock;
  scheduler: Scheduler;
  /**
   * Headers every attempt carries beyond the ones the request builder sets —
   * authorization, and `Accept-Language` so a refusal comes back in the
   * language the person is reading (FR-UX-007). A function, not a value: an
   * upload may happen an hour after the queue was built, by which time the
   * token has been refreshed and the language may have been switched.
   */
  headers?: () => Readonly<Record<string, string>>;
  random?: () => number;
}

export class QueueUploader {
  private readonly options: QueueUploaderOptions;
  private unsubscribe: Cancel | null = null;
  private timer: Cancel | null = null;
  private inFlight: Promise<void> | null = null;
  private askedAgain = false;
  private started = false;
  private stopped = false;

  constructor(options: QueueUploaderOptions) {
    this.options = options;
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    this.stopped = false;
    this.options.queue.setOffline(!this.options.connectivity.online);
    this.unsubscribe = this.options.connectivity.onOnline(() => this.drain());
    void this.drain();
  }

  stop(): void {
    this.started = false;
    this.stopped = true;
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.clearTimer();
  }

  /**
   * Send everything that is due, oldest capture first.
   *
   * Sequential rather than parallel, and that is a decision rather than
   * simplicity: `capture_page` allocates a receipt's position as max + 1
   * (ADR-031), so parallel uploads would number a batch in whatever order the
   * responses happened to land — against a pile of paper somebody is checking
   * it against. It is also the kinder thing to do to the connection that has
   * just come back.
   */
  async drain(): Promise<void> {
    if (this.inFlight !== null) {
      // Not dropped: recorded and awaited. A caller that asked for a drain is
      // entitled to assume one happened by the time its promise settles —
      // returning early would make a retry button that appears to do nothing
      // whenever a timer fired a moment earlier.
      this.askedAgain = true;
      await this.inFlight;
      return;
    }

    this.inFlight = (async () => {
      try {
        do {
          this.askedAgain = false;
          await this.pass();
        } while (this.askedAgain);
      } finally {
        this.inFlight = null;
      }
    })();
    await this.inFlight;
  }

  private async pass(): Promise<void> {
    this.clearTimer();

    const online = this.options.connectivity.online;
    this.options.queue.setOffline(!online);
    if (!online) {
      // Nothing to arm. The online event is what ends this state, and a timer
      // that woke every minute to observe that the phone is still in a tunnel
      // would cost battery to learn nothing.
      return;
    }

    for (const record of due(await this.options.queue.records(), this.options.clock.now())) {
      const attempted = await this.options.queue.markUploading(record);
      const { payload, image } = await this.options.queue.open(attempted);
      const response = await this.options.transport.send(
        buildRequest(payload, image, {
          resolvedItemId: attempted.resolvedItemId,
          headers: this.options.headers?.() ?? {},
        }),
      );
      const outcome = classify(response);

      if (outcome.kind === "delivered") {
        // The page that created the receipt carries back the id every other
        // page of it needs. Resolved BEFORE the record is removed, so a crash
        // between the two leaves the siblings waiting — recoverable — rather
        // than released with nothing to join.
        if (attempted.pageIndex === 0 && response.kind === "response" && response.itemId !== null) {
          await this.options.queue.resolveReceipt(attempted.receiptRef, response.itemId);
          // The due list for this pass was computed before that resolution, so
          // the pages it just released are not in it. Asking for another pass
          // is what picks them up — without this, a receipt's remaining pages
          // would wait for the next timer or the next online event, which on a
          // connection that is about to drop again is the difference between
          // one expense and two.
          this.askedAgain = true;
        }
        await this.options.queue.markDelivered(attempted);
        continue;
      }
      if (outcome.kind === "blocked") {
        await this.options.queue.markBlocked(attempted, outcome.reason);
        continue;
      }

      await this.options.queue.markWaiting(
        attempted,
        this.options.clock.now() + backoffMs(attempted.attempts, this.options.random),
      );

      if (response.kind === "network_error") {
        // The connection is gone, whatever the platform's flag says. Offering
        // it the other thirty receipts would fail thirty times, spend thirty
        // records' worth of backoff, and move nothing.
        this.options.queue.setOffline(true);
        break;
      }
    }

    await this.arm();
  }

  /**
   * One timer for the whole queue, set to whenever the next record is due —
   * not one timer per record, so forty waiting receipts are one pending
   * callback.
   *
   * Armed by `drain` itself rather than only by `start`, so a queue drained
   * directly — a retry button, an app resume — still schedules its own
   * follow-up. `stop` is the only thing that suppresses it.
   */
  private async arm(): Promise<void> {
    if (this.stopped) return;
    const at = nextDueAt(await this.options.queue.records());
    if (at === null) return;
    this.clearTimer();
    this.timer = this.options.scheduler.schedule(Math.max(0, at - this.options.clock.now()), () => {
      this.timer = null;
      return this.drain();
    });
  }

  private clearTimer(): void {
    this.timer?.();
    this.timer = null;
  }
}
