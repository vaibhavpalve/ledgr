import { describe, expect, it } from "vitest";
import { frame, unframe } from "@ledgr/offline-queue";
import type { SealedPayload } from "@ledgr/offline-queue";

import { MemoryKeyVault, QueueKeyMissing, WebCryptoCipher } from "./webCryptoCipher";

/**
 * Against real WebCrypto, not a stub. An encryption test that mocks the
 * encryption tests nothing — MOB-009 is a claim about what is on the device,
 * and only the real primitive can support it.
 */

const payload: SealedPayload = {
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
  idempotencyKey: "key-1",
  capturedAt: "2026-09-06T10:00:00.000Z",
};

const image = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46]);

describe("WebCryptoCipher — MOB-009", () => {
  it("round-trips a sealed capture", async () => {
    const cipher = new WebCryptoCipher(new MemoryKeyVault());

    const sealed = await cipher.seal("record-1", frame(payload, image));
    const { payload: back, image: imageBack } = unframe(await cipher.open("record-1", sealed));

    expect(back).toEqual(payload);
    expect([...imageBack]).toEqual([...image]);
  });

  it("leaves neither the image nor the tenant context readable in the ciphertext", async () => {
    const cipher = new WebCryptoCipher(new MemoryKeyVault());

    const sealed = await cipher.seal("record-1", frame(payload, image));

    expect(new TextDecoder().decode(sealed.ciphertext)).not.toContain("adm-A");
    expect(new TextDecoder().decode(sealed.ciphertext)).not.toContain("receipt.jpg");
    expect([...sealed.ciphertext].join(",")).not.toContain([...image].join(","));
  });

  it("generates a key that cannot be exported, by this code or any other", async () => {
    // The property that makes IndexedDB a safe place for it. An extractable
    // key in a browser store is a key any script on the origin can read out
    // and post somewhere, which is no protection at all.
    const vault = new MemoryKeyVault();
    const cipher = new WebCryptoCipher(vault);
    await cipher.seal("record-1", new Uint8Array([1]));

    const key = await vault.load();

    expect(key?.extractable).toBe(false);
    await expect(crypto.subtle.exportKey("raw", key!)).rejects.toThrow();
  });

  it("uses a fresh nonce for every capture", async () => {
    // Reusing a nonce under one AES-GCM key is the failure that leaks
    // plaintext, and a queue seals dozens of records under a single key.
    const cipher = new WebCryptoCipher(new MemoryKeyVault());
    const plaintext = new Uint8Array([1, 2, 3]);

    const first = await cipher.seal("record-1", plaintext);
    const second = await cipher.seal("record-2", plaintext);

    expect([...first.iv]).not.toEqual([...second.iv]);
    expect([...first.ciphertext]).not.toEqual([...second.ciphertext]);
  });

  it("refuses to open an envelope under a different record id", async () => {
    // The AAD binding. Without it, someone with write access to the device
    // store could swap two envelopes and have a receipt upload under another
    // record's administration.
    const cipher = new WebCryptoCipher(new MemoryKeyVault());
    const sealed = await cipher.seal("record-1", frame(payload, image));

    await expect(cipher.open("record-2", sealed)).rejects.toThrow();
  });

  it("refuses to open a ciphertext that has been altered", async () => {
    const cipher = new WebCryptoCipher(new MemoryKeyVault());
    const sealed = await cipher.seal("record-1", frame(payload, image));
    const tampered = Uint8Array.from(sealed.ciphertext);
    tampered.set([(tampered[0] ?? 0) ^ 0xff], 0);

    await expect(cipher.open("record-1", { ...sealed, ciphertext: tampered })).rejects.toThrow();
  });

  it("keeps using one key, so records sealed earlier stay readable", async () => {
    const vault = new MemoryKeyVault();
    const sealed = await new WebCryptoCipher(vault).seal("record-1", frame(payload, image));

    // A fresh cipher over the same vault: what a page reload looks like.
    const afterReload = new WebCryptoCipher(vault);

    expect(unframe(await afterReload.open("record-1", sealed)).payload).toEqual(payload);
  });

  it("makes every sealed capture unreadable the moment the key is destroyed", async () => {
    // MOB-009's purge, and the reason CaptureQueue.purge destroys the key
    // before it deletes anything: what survives an interrupted delete is
    // noise.
    const vault = new MemoryKeyVault();
    const cipher = new WebCryptoCipher(vault);
    const sealed = await cipher.seal("record-1", frame(payload, image));

    await cipher.destroyKey();

    await expect(cipher.open("record-1", sealed)).rejects.toThrow(QueueKeyMissing);
  });

  it("does not quietly mint a new key to open records sealed under the old one", async () => {
    // Generating on demand would "succeed" and produce garbage, and the
    // failure would surface much later as a corrupt image.
    const vault = new MemoryKeyVault();
    const cipher = new WebCryptoCipher(vault);
    const sealed = await cipher.seal("record-1", frame(payload, image));
    await cipher.destroyKey();

    await expect(cipher.open("record-1", sealed)).rejects.toThrow(QueueKeyMissing);
    expect(await vault.load()).toBeNull();
  });

  it("seals again after a purge, under a key that cannot open the old records", async () => {
    const vault = new MemoryKeyVault();
    const cipher = new WebCryptoCipher(vault);
    const before = await cipher.seal("record-1", frame(payload, image));
    await cipher.destroyKey();

    const after = await cipher.seal("record-1", frame(payload, image));

    expect(unframe(await cipher.open("record-1", after)).payload).toEqual(payload);
    await expect(cipher.open("record-1", before)).rejects.toThrow();
  });
});
