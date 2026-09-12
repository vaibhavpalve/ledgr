/**
 * Where a queued capture actually sits on the device — MOB-003, MOB-009.
 *
 * IndexedDB, and not `localStorage`, for three reasons that all matter:
 * it stores binary without a base64 detour that would cost a third of the
 * space; it holds a live non-extractable `CryptoKey`, which is what keeps the
 * key out of reach of script (see webCryptoCipher.ts); and it is not capped at
 * a few megabytes, which a queue of receipt photographs would exhaust
 * immediately.
 *
 * Two stores in one database, so that a purge and a schema upgrade touch one
 * thing:
 *
 *     captures   one record per queued capture, keyed by its id
 *     key        exactly one entry, the queue's AES key
 *
 * This module is deliberately thin — open, put, get, delete, clear — because
 * it is the one piece of the queue that cannot be tested here: jsdom has no
 * IndexedDB, and pulling in a fake implementation to test five one-line
 * wrappers would be more machinery than the thing it checks. Everything with a
 * decision in it lives in `@ledgr/offline-queue`, over the `QueueStore` port,
 * and is tested there against an in-memory store.
 */

import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";

import type { KeyVault } from "./webCryptoCipher";

const DATABASE = "ledgr-capture-queue";
const VERSION = 1;
const CAPTURES = "captures";
const KEYS = "key";
const KEY_ID = "queue";

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(CAPTURES))
        db.createObjectStore(CAPTURES, { keyPath: "id" });
      if (!db.objectStoreNames.contains(KEYS)) db.createObjectStore(KEYS);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/**
 * One transaction, awaited to COMPLETION rather than to the request's success.
 *
 * The distinction is the whole reason this helper exists. An IndexedDB request
 * succeeds before its transaction commits, so resolving on `onsuccess` would
 * report a capture as queued while the write could still be rolled back — and
 * a queue that says "saved" about a photograph that was not is the failure
 * this entire module is built to avoid.
 */
async function transact<T>(
  store: string,
  mode: IDBTransactionMode,
  work: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const db = await open();
  try {
    return await new Promise<T>((resolve, reject) => {
      const transaction = db.transaction(store, mode);
      const request = work(transaction.objectStore(store));
      transaction.oncomplete = () => resolve(request.result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  } finally {
    db.close();
  }
}

export class IndexedDbQueueStore implements QueueStore {
  async put(record: StoredCapture): Promise<void> {
    await transact(CAPTURES, "readwrite", (store) => store.put(record));
  }

  async all(): Promise<readonly StoredCapture[]> {
    return transact<StoredCapture[]>(CAPTURES, "readonly", (store) => store.getAll());
  }

  async remove(id: string): Promise<void> {
    await transact(CAPTURES, "readwrite", (store) => store.delete(id));
  }

  async clear(): Promise<void> {
    await transact(CAPTURES, "readwrite", (store) => store.clear());
  }
}

/**
 * The AES key, stored as a live `CryptoKey`.
 *
 * IndexedDB structured-clones it, so a non-extractable key survives a reload
 * still non-extractable. There is no encoding step here and there must not be:
 * anything that turned the key into bytes would be the moment it stopped being
 * protected.
 */
export class IndexedDbKeyVault implements KeyVault {
  async load(): Promise<CryptoKey | null> {
    return (
      (await transact<CryptoKey | undefined>(KEYS, "readonly", (store) => store.get(KEY_ID))) ??
      null
    );
  }

  async save(key: CryptoKey): Promise<void> {
    await transact(KEYS, "readwrite", (store) => store.put(key, KEY_ID));
  }

  async clear(): Promise<void> {
    await transact(KEYS, "readwrite", (store) => store.delete(KEY_ID));
  }
}

/** True where the queue can persist at all — private modes can refuse it. */
export function storageAvailable(): boolean {
  return typeof indexedDB !== "undefined";
}

/**
 * Ask the browser not to evict this origin's storage.
 *
 * Without it, IndexedDB is "best-effort": a browser under storage pressure may
 * clear an origin's data with no warning and no event, which for this queue
 * means a photograph somebody believes is saved disappearing on the way home.
 * MOB-009 caps what the queue may hold precisely so this stays a small ask.
 *
 * Returns whether the browser agreed. It may say no — the decision is the
 * browser's, based on engagement heuristics this code cannot influence — and a
 * no is not a failure: the queue works either way, and this is the difference
 * between "will not be evicted" and "probably will not be". Best-effort by
 * design, so it never blocks a capture.
 */
export async function requestDurableStorage(): Promise<boolean> {
  try {
    if (typeof navigator === "undefined" || navigator.storage?.persist === undefined) return false;
    return await navigator.storage.persist();
  } catch {
    return false;
  }
}
