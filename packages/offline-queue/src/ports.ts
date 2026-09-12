/**
 * What the queue needs from a platform, and nothing more — MOB-003, MOB-009.
 *
 * Five small interfaces. A browser satisfies them with IndexedDB, WebCrypto,
 * `navigator.onLine`, `fetch` and `setTimeout`; React Native satisfies them
 * with SQLCipher, the platform keystore, NetInfo and its own fetch. Neither
 * implementation gets to hold any policy: the cap, the backoff, the purge
 * rules and the request shape are all on this side of the line.
 */

import type { Bytes, PurgeReason, SealedBlob, StoredCapture } from "./model";

/**
 * Persistence for queued captures.
 *
 * Deliberately tiny, and deliberately not a query language. The queue is
 * capped, so reading it whole is cheap, and every selection — what is due,
 * what is blocked, how many bytes are held — is a pure function over that list
 * where it can be tested without a database.
 */
export interface QueueStore {
  put(record: StoredCapture): Promise<void>;
  all(): Promise<readonly StoredCapture[]>;
  remove(id: string): Promise<void>;
  /** Every record, gone. MOB-009's purge. */
  clear(): Promise<void>;
}

/**
 * MOB-009's encryption, as the only way bytes reach the store.
 *
 * `seal` and `open` take the record id and bind it into the ciphertext as
 * additional authenticated data, so a ciphertext cannot be moved from one
 * record to another. Without that, an attacker with write access to the device
 * store could swap two records' envelopes and have a receipt upload under a
 * different record's metadata — including a different administration's.
 *
 * `destroyKey` is what makes a purge instant and complete. See
 * `CaptureQueue.purge`.
 */
export interface QueueCipher {
  seal(recordId: string, plaintext: Bytes): Promise<SealedBlob>;
  open(recordId: string, sealed: SealedBlob): Promise<Bytes>;
  destroyKey(): Promise<void>;
}

/** Epoch millis. Injected so backoff is testable without waiting for it. */
export interface Clock {
  now(): number;
}

export type Cancel = () => void;

/**
 * Work that may be asynchronous. `setTimeout` and an event listener both
 * ignore what comes back; a test scheduler awaits it, which is what makes
 * "the timer fired and the drain finished" one step instead of a sleep.
 */
export type Wakeup = () => void | Promise<void>;

/** A single delayed callback. `setTimeout` on both platforms; a fake in tests. */
export interface Scheduler {
  schedule(afterMs: number, run: Wakeup): Cancel;
}

/**
 * Whether the device believes it has a connection.
 *
 * `online` is a hint and is treated as one: the uploader attempts a drain when
 * it flips to true, and treats a network failure while it says true as an
 * ordinary retryable error. `navigator.onLine` reports an interface being up,
 * not the API being reachable, and a captive portal satisfies it.
 */
export interface Connectivity {
  readonly online: boolean;
  /** Called when connectivity is regained. Returns an unsubscribe. */
  onOnline(listener: Wakeup): Cancel;
}

/**
 * One capture upload, as the platform performs it.
 *
 * Takes a fully-built request (see request.ts) and returns a classified
 * outcome. Classification lives with the request builder rather than in each
 * platform's transport: what a 415 means is API knowledge, and two transports
 * classifying it independently is two chances to retry something forever that
 * will never succeed.
 */
export interface Transport {
  send(request: CaptureRequest): Promise<TransportResponse>;
}

export interface CaptureRequest {
  readonly method: "POST";
  /** Path and query, already carrying the record's own administration id. */
  readonly path: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: Bytes;
}

/**
 * The raw result. `status` is absent when the request never reached a server —
 * which is the normal case in this queue and is why it is modelled rather than
 * flattened into a status code.
 *
 * `reason` is the machine-readable field `api.i18n.http.problem` writes beside
 * the human sentence. A transport lifts it out of the body and passes it on
 * without interpreting it; `classify` is what reads it. Null when the body
 * carried none — which is itself informative, since the idempotency
 * middleware's conflicts are exactly the ones with no reason.
 */
export type TransportResponse =
  | {
      readonly kind: "response";
      readonly status: number;
      readonly reason: string | null;
      /**
       * `item_id` from a successful `capture_page`: the server's id for the
       * receipt this page landed on.
       *
       * The queue needs it to send the receipt's REMAINING pages, which were
       * photographed before any of this existed. Null on a failure, and null
       * where the body could not be read — a delivered page whose item id was
       * lost leaves its siblings unable to join it, which the uploader reports
       * rather than papering over.
       */
      readonly itemId: string | null;
    }
  | { readonly kind: "network_error" };

export interface PurgeEvent {
  readonly reason: PurgeReason;
  readonly discarded: number;
}
