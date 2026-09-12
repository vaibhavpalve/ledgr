/**
 * The web app's offline capture queue, assembled — FR-EXP-001f, MOB-003,
 * MOB-009.
 *
 * This is the composition root and the only file in the app that knows which
 * adapter fills which port. Everything above it — the capture screen when it
 * lands, and `CaptureQueueStatus` today — talks to `CaptureQueue` and
 * `QueueUploader`, which are platform-free.
 *
 * --- Why the queue is a module singleton ---
 *
 * There is exactly one queue per device, because there is exactly one
 * IndexedDB database and one key. Two `CaptureQueue` objects over the same
 * store would each hold their own delivered counter and their own listener
 * set, and a purge through one would leave the other reporting captures it can
 * no longer read. Built lazily rather than at import so that a test, and a
 * browser without IndexedDB, are not forced to open a database to load this
 * module.
 *
 * --- MOB-009's three purge triggers ---
 *
 * `purgeCaptureQueue` is the single entry point for all three, so a fourth
 * caller cannot invent a purge that forgets the key. Only one of them is
 * reachable today, and honestly so:
 *
 *   logout       there is no sign-out flow yet (see auth/SignInPending), so
 *                nothing calls this. When one lands it calls this BEFORE
 *                clearing the session, and it warns first — see
 *                `capturesAtRisk`.
 *   role_change  the client switcher is the place this belongs; the endpoint
 *                that would report a changed grant is not built.
 *   remote_wipe  server-initiated, and the signal for it does not exist yet.
 *
 * Naming all three now is deliberate: the mechanism is one function, and the
 * three call sites are then wiring rather than three chances to write a purge
 * that leaves the key behind.
 */

import {
  CaptureQueue,
  QueueUploader,
  type PurgeEvent,
  type PurgeReason,
} from "@ledgr/offline-queue";

import { languageHeaders } from "../i18n";
import { BrowserConnectivity } from "./browserConnectivity";
import { FetchTransport } from "./fetchTransport";
import { IndexedDbKeyVault, IndexedDbQueueStore, requestDurableStorage } from "./indexedDb";
import { WebCryptoCipher } from "./webCryptoCipher";
import type { Language } from "@ledgr/i18n";

let queue: CaptureQueue | null = null;
let uploader: QueueUploader | null = null;

export function captureQueue(): CaptureQueue {
  queue ??= new CaptureQueue({
    store: new IndexedDbQueueStore(),
    cipher: new WebCryptoCipher(new IndexedDbKeyVault()),
    clock: { now: () => Date.now() },
    newId: () => crypto.randomUUID(),
  });
  return queue;
}

/**
 * Starts draining, and keeps draining, for as long as the app is running.
 *
 * `language` is read through a callback rather than captured, because an
 * upload may happen an hour after this was called — by which time the person
 * may have switched language, and FR-UX-007 wants any refusal to come back in
 * the one they are actually reading.
 */
export function startCaptureUploads(currentLanguage: () => Language): QueueUploader {
  // Asked for once, early, and not waited on. An origin whose storage is
  // best-effort can be cleared by the browser with no warning and no event,
  // which for this queue means a photograph vanishing between the car park and
  // home. The browser may refuse; the queue works either way.
  void requestDurableStorage();

  uploader ??= new QueueUploader({
    queue: captureQueue(),
    transport: new FetchTransport(),
    connectivity: new BrowserConnectivity(),
    clock: { now: () => Date.now() },
    scheduler: {
      schedule(afterMs, run) {
        const handle = setTimeout(() => void run(), afterMs);
        return () => clearTimeout(handle);
      },
    },
    headers: () => languageHeaders(currentLanguage()),
  });
  uploader.start();
  return uploader;
}

/**
 * MOB-009: "purged on logout, role change or remote wipe."
 *
 * Stops the uploader before purging. An attempt already in flight holds a
 * decrypted image in memory and would, on success, delete a record that a
 * purge is about to delete anyway — harmless — but on failure it would write a
 * fresh record back into a store that was supposed to be empty. Stopping first
 * removes the race rather than reasoning about it.
 */
export async function purgeCaptureQueue(reason: PurgeReason): Promise<PurgeEvent> {
  uploader?.stop();
  uploader = null;
  const event = await captureQueue().purge(reason);
  queue = null;
  return event;
}

/**
 * How many captures a sign-out would destroy right now.
 *
 * A sign-out flow calls this and says so before it offers the button. The
 * purge is what MOB-009 requires; taking somebody's four unposted receipts
 * without telling them is not, and the warning costs one sentence.
 */
export async function capturesAtRisk(): Promise<number> {
  return captureQueue().pending();
}
