/**
 * MOB-009's encryption, in a browser — AES-256-GCM over WebCrypto.
 *
 *   MOB-009  No financial data written to unencrypted device storage;
 *            on-device cache is encrypted, size-capped and purged on logout,
 *            role change or remote wipe.
 *
 * --- The key is generated non-extractable and never leaves the browser ---
 *
 * `generateKey(..., false, ...)` produces a `CryptoKey` that `exportKey` will
 * refuse, for this code and for anything else running on the origin. It is
 * stored as a live `CryptoKey` object — IndexedDB structured-clones it — so
 * the raw bytes are never a JavaScript value at any point in its life. Script
 * on the page can ask it to decrypt; script on the page cannot read it, copy
 * it out, or send it anywhere.
 *
 * That is the browser's nearest equivalent to the platform keystore MOB-008
 * names for the mobile apps, and it is why the key lives in IndexedDB rather
 * than in `localStorage` as base64 — which would be a key in cleartext next to
 * the ciphertext it opens, and no encryption at all.
 *
 * --- A missing key is an error, never a new key ---
 *
 * `open` refuses when the vault is empty. Generating one on demand would
 * "succeed" at decrypting queued records into garbage, and the failure would
 * surface as a corrupt image somewhere downstream. An empty vault beside
 * existing records means a purge happened, and the honest answer is that those
 * records are gone.
 */

import type { Bytes, QueueCipher, SealedBlob } from "@ledgr/offline-queue";

/** Where the key lives. A port, so a test can hold it in memory. */
export interface KeyVault {
  load(): Promise<CryptoKey | null>;
  save(key: CryptoKey): Promise<void>;
  clear(): Promise<void>;
}

/** GCM's recommended nonce size. Fresh per record; never reused. */
const IV_BYTES = 12;

/**
 * Bound into every envelope as additional authenticated data, so a ciphertext
 * cannot be moved to another record. Versioned, because the day the framing
 * changes an old envelope must fail to open rather than open as nonsense.
 */
function additionalData(recordId: string): Bytes {
  return new TextEncoder().encode(`ledgr.capture.v1:${recordId}`);
}

export class QueueKeyMissing extends Error {}

export class WebCryptoCipher implements QueueCipher {
  /**
   * Held in memory so a drain of forty records is one IndexedDB read rather
   * than forty. Dropped by `destroyKey`, which is what makes the purge take
   * effect for work already in flight and not only for the next read.
   */
  private cached: Promise<CryptoKey> | null = null;

  constructor(
    private readonly vault: KeyVault,
    private readonly subtle: SubtleCrypto = crypto.subtle,
    private readonly randomBytes: (length: number) => Bytes = (length) =>
      crypto.getRandomValues(new Uint8Array(length)),
  ) {}

  async seal(recordId: string, plaintext: Bytes): Promise<SealedBlob> {
    const iv = this.randomBytes(IV_BYTES);
    const ciphertext = await this.subtle.encrypt(
      { name: "AES-GCM", iv, additionalData: additionalData(recordId) },
      await this.key(),
      plaintext,
    );
    return { iv, ciphertext: new Uint8Array(ciphertext) };
  }

  async open(recordId: string, sealed: SealedBlob): Promise<Bytes> {
    const key = await this.vault.load();
    if (key === null) {
      throw new QueueKeyMissing(
        "the capture queue's key is gone, so its records cannot be read. This " +
          "is what a purge leaves behind (MOB-009).",
      );
    }
    const plaintext = await this.subtle.decrypt(
      { name: "AES-GCM", iv: sealed.iv, additionalData: additionalData(recordId) },
      key,
      sealed.ciphertext,
    );
    return new Uint8Array(plaintext);
  }

  /**
   * MOB-009's purge, done in one operation.
   *
   * Every queued record becomes permanently unreadable the moment this
   * returns, whatever happens to the record rows afterwards — which is exactly
   * why `CaptureQueue.purge` calls this first and deletes the rows second.
   */
  async destroyKey(): Promise<void> {
    this.cached = null;
    await this.vault.clear();
  }

  private key(): Promise<CryptoKey> {
    this.cached ??= (async () => {
      const existing = await this.vault.load();
      if (existing !== null) return existing;
      const created = await this.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, [
        "encrypt",
        "decrypt",
      ]);
      await this.vault.save(created as CryptoKey);
      return created as CryptoKey;
    })();
    return this.cached;
  }
}

/** For tests, and for nothing else — a browser vault is IndexedDB. */
export class MemoryKeyVault implements KeyVault {
  private key: CryptoKey | null = null;

  async load(): Promise<CryptoKey | null> {
    return this.key;
  }

  async save(key: CryptoKey): Promise<void> {
    this.key = key;
  }

  async clear(): Promise<void> {
    this.key = null;
  }
}
