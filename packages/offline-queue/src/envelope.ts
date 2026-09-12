/**
 * One capture, as one sequence of bytes to encrypt — MOB-009.
 *
 *     [ 4-byte big-endian header length ][ UTF-8 JSON header ][ image bytes ]
 *
 * Metadata and image are sealed TOGETHER, in one envelope, rather than the
 * image being encrypted beside a cleartext row. Two reasons, and the second is
 * the one that decided it:
 *
 *   1. The metadata is not innocuous. Which client, which fiscal year, which
 *      person, which file name: a device holding that in the clear is holding
 *      a record of whose books have been worked on, and MOB-009 draws its line
 *      at device storage rather than at any particular field.
 *   2. One envelope cannot be half-decrypted. Metadata and image are
 *      authenticated as a unit, so an image cannot be paired with another
 *      record's routing information — the failure that would upload a receipt
 *      into the wrong administration.
 *
 * The framing is length-prefixed rather than, say, base64 in a JSON field. A
 * 25 MiB photograph base64'd is 33 MiB, and it would be encoded on capture and
 * decoded on every upload attempt: real cost, on a phone, for nothing.
 */

import type { Bytes, SealedPayload } from "./model";

const HEADER_LENGTH_BYTES = 4;

/** Guards against a corrupt length prefix asking for a gigabyte-long header. */
const MAX_HEADER_BYTES = 64 * 1024;

export class CorruptEnvelope extends Error {}

export function frame(payload: SealedPayload, image: Bytes): Bytes {
  const header = new TextEncoder().encode(JSON.stringify(payload));
  const framed = new Uint8Array(HEADER_LENGTH_BYTES + header.length + image.length);
  new DataView(framed.buffer).setUint32(0, header.length, false);
  framed.set(header, HEADER_LENGTH_BYTES);
  framed.set(image, HEADER_LENGTH_BYTES + header.length);
  return framed;
}

export function unframe(plaintext: Bytes): { payload: SealedPayload; image: Bytes } {
  if (plaintext.length < HEADER_LENGTH_BYTES) {
    throw new CorruptEnvelope("capture envelope is shorter than its length prefix");
  }

  const view = new DataView(plaintext.buffer, plaintext.byteOffset, plaintext.byteLength);
  const headerLength = view.getUint32(0, false);
  const imageStart = HEADER_LENGTH_BYTES + headerLength;

  if (headerLength > MAX_HEADER_BYTES || imageStart > plaintext.length) {
    throw new CorruptEnvelope(
      `capture envelope declares a ${headerLength}-byte header it does not contain`,
    );
  }

  const header = plaintext.subarray(HEADER_LENGTH_BYTES, imageStart);
  // `slice`, not `subarray`: the image is handed to a transport that may
  // outlive this call, and a view would keep the whole decrypted envelope —
  // metadata included — alive behind it.
  return {
    payload: JSON.parse(new TextDecoder().decode(header)) as SealedPayload,
    image: plaintext.slice(imageStart),
  };
}
