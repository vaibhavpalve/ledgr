/**
 * In-memory ports, for tests.
 *
 * Not exported from index.ts on purpose: these are doubles, and a double
 * reachable from the package entry point is a double somebody eventually ships
 * — a queue that "encrypts" with a reversible XOR would satisfy MOB-009 in
 * every test and in nothing else.
 *
 * `FakeCipher` is honest about being fake. It records what it was asked to
 * seal and refuses to open a blob under a different record id, which is the
 * property `QueueCipher`'s AAD binding exists for; it makes no attempt to be
 * confidential. The real thing is tested against real WebCrypto in
 * apps/web/src/capture.
 */

import type { Bytes, SealedBlob, StoredCapture } from "./model";
import type { Clock, Connectivity, QueueCipher, QueueStore, Scheduler, Transport } from "./ports";
import type { CaptureRequest, TransportResponse, Wakeup } from "./ports";

export class MemoryStore implements QueueStore {
  private readonly records = new Map<string, StoredCapture>();

  async put(record: StoredCapture): Promise<void> {
    this.records.set(record.id, record);
  }

  async all(): Promise<readonly StoredCapture[]> {
    return [...this.records.values()];
  }

  async remove(id: string): Promise<void> {
    this.records.delete(id);
  }

  async clear(): Promise<void> {
    this.records.clear();
  }
}

export class KeyDestroyed extends Error {}

export class FakeCipher implements QueueCipher {
  private live = true;
  /** Every `seal`, so a test can assert nothing reached the store unsealed. */
  readonly sealed: Bytes[] = [];

  async seal(recordId: string, plaintext: Bytes): Promise<SealedBlob> {
    if (!this.live) throw new KeyDestroyed("the queue key has been destroyed");
    this.sealed.push(plaintext);
    return {
      iv: new TextEncoder().encode(recordId),
      // Reversed, so a test asserting "the store does not hold the plaintext"
      // is asserting something rather than passing by accident.
      ciphertext: Uint8Array.from(plaintext).reverse(),
    };
  }

  async open(recordId: string, sealed: SealedBlob): Promise<Bytes> {
    if (!this.live) throw new KeyDestroyed("the queue key has been destroyed");
    const boundTo = new TextDecoder().decode(sealed.iv);
    if (boundTo !== recordId) {
      throw new Error(`envelope is bound to ${boundTo}, not ${recordId}`);
    }
    return Uint8Array.from(sealed.ciphertext).reverse();
  }

  async destroyKey(): Promise<void> {
    this.live = false;
  }
}

export class TestClock implements Clock {
  constructor(private millis = 1_700_000_000_000) {}

  now(): number {
    return this.millis;
  }

  advance(by: number): void {
    this.millis += by;
  }
}

/** Timers that fire when a test says so, never on their own. */
export class TestScheduler implements Scheduler {
  private pending: { at: number; run: Wakeup; cancelled: boolean }[] = [];

  constructor(private readonly clock: TestClock) {}

  schedule(afterMs: number, run: Wakeup): () => void {
    const entry = { at: this.clock.now() + afterMs, run, cancelled: false };
    this.pending.push(entry);
    return () => {
      entry.cancelled = true;
    };
  }

  get armed(): number {
    return this.pending.filter((entry) => !entry.cancelled).length;
  }

  /**
   * Move time forward, run whatever that made due, and WAIT for it.
   *
   * Awaiting the callback is what lets a test say "the backoff elapsed and the
   * retry happened" as one step. The alternative — firing and then polling —
   * is where flaky suites come from.
   */
  async runDueAfter(advanceBy: number): Promise<void> {
    this.clock.advance(advanceBy);
    const now = this.clock.now();
    const firing = this.pending.filter((entry) => !entry.cancelled && entry.at <= now);
    this.pending = this.pending.filter((entry) => !firing.includes(entry));
    for (const entry of firing) await entry.run();
  }
}

export class TestConnectivity implements Connectivity {
  private listeners = new Set<Wakeup>();

  constructor(public online = true) {}

  onOnline(listener: Wakeup): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  goOffline(): void {
    this.online = false;
  }

  async goOnline(): Promise<void> {
    this.online = true;
    for (const listener of [...this.listeners]) await listener();
  }
}

/** Answers each request from a scripted list; falls back to 201. */
export class TestTransport implements Transport {
  readonly sent: CaptureRequest[] = [];

  constructor(private readonly responses: TransportResponse[] = []) {}

  async send(request: CaptureRequest): Promise<TransportResponse> {
    this.sent.push(request);
    return this.responses.shift() ?? { kind: "response", status: 201, reason: null, itemId: null };
  }
}

let counter = 0;

/**
 * Predictable ids, so a failure names a record rather than a uuid.
 *
 * Fixed width, which matters more than it looks: the idempotency key is inside
 * the sealed payload, so an id that grew from `id-000009` to `id-0000010`
 * would change the envelope's length — and a cap test measuring one record
 * against another would drift by a byte.
 */
export function testIds(prefix = "id"): () => string {
  return () => `${prefix}-${String(++counter).padStart(6, "0")}`;
}
