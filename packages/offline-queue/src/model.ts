/**
 * The offline capture queue's vocabulary — FR-EXP-001f, MOB-003, MOB-009.
 *
 *   FR-EXP-001f  Capture works with no connectivity: the image is queued
 *                encrypted on-device and uploads automatically, with visible
 *                queue state (see MOB-003).
 *   MOB-003      Offline capture queue — documents taken without connectivity
 *                are stored encrypted on-device and uploaded when connectivity
 *                returns, with visible queue state.
 *   MOB-009      No financial data written to unencrypted device storage;
 *                on-device cache is encrypted, size-capped and purged on
 *                logout, role change or remote wipe.
 *
 * --- Why this package has no browser and no React Native in it ---
 *
 * Everything here is platform-free, the way `@ledgr/i18n` is: no `crypto`, no
 * `navigator`, no `fetch`, no `IndexedDB`, no timers. The tsconfig omits the
 * DOM lib so that stays true rather than being remembered. Encryption,
 * storage, connectivity and transport arrive as ports (see ports.ts), and the
 * web app and the React Native app (MOB-001) supply their own.
 *
 * The reason is the one CLAUDE.md gives for the authorization library: a queue
 * whose policy — the size cap, the purge triggers, the retry, the tenant
 * binding — lived inside the web app would be re-derived from scratch by the
 * mobile app, and the second implementation is the one that forgets to purge
 * on a role change.
 *
 * --- Three states, and no `uploaded` ---
 *
 *     queued     waiting. Either never attempted, or waiting out a backoff.
 *     uploading  in flight right now.
 *     blocked    the server refused in a way retrying cannot fix. A person has
 *                to look at it.
 *
 * A delivered capture is DELETED, not marked. Its bytes are on the server, and
 * keeping a second copy on the device would spend MOB-009's cap on work
 * already done — the cap exists to bound what is undelivered. The count of
 * deliveries is reported through the snapshot instead, so a screen can still
 * say "3 of 8 uploaded" while a drain runs.
 *
 * `blocked` items keep their bytes and keep counting against the cap. They are
 * the person's photograph of a receipt they may no longer be holding, and
 * discarding them automatically to free space would destroy evidence to make
 * room for evidence. Discarding is theirs to do, which is why the queue
 * display names a reason and offers the action.
 */

/**
 * Binary, backed by a plain `ArrayBuffer`.
 *
 * The buffer is pinned in the type rather than left as `ArrayBufferLike`
 * because every one of these bytes ends up in a `SubtleCrypto` call or a
 * `fetch` body, and both refuse a possibly-shared buffer. Saying so once here
 * is the alternative to a cast at each of those boundaries — and a cast at a
 * crypto boundary is the kind of thing that is still there when the assumption
 * behind it stops holding.
 */
export type Bytes = Uint8Array<ArrayBuffer>;

/** How a page arrived. Mirrors `api.expenses.model.CaptureSource` exactly. */
export type CaptureSource = "camera" | "upload";

/**
 * Runtime arrays, with the types derived from them rather than the other way
 * round — the shape `CLIENT_COLOURS` uses in `@ledgr/shared-types`.
 *
 * A queue display renders these through catalogue keys it builds at runtime
 * (`capture.queue.reason.${reason}`), which the translation check cannot see as
 * string literals. Exporting the values is what lets a test walk them and fail
 * the build on a member with no message, instead of a raw key reaching a
 * person's screen (FR-UX-007).
 */
export const QUEUE_STATES = ["queued", "uploading", "blocked"] as const;

export type QueueState = (typeof QUEUE_STATES)[number];

/**
 * Why the server will never accept this capture, however many times it is
 * offered. Each maps to a sentence in the message catalogue — a queue that
 * shows "failed" and stops has told the person nothing they can act on.
 *
 * These are the refusals `api.expenses.routes.capture_page` can give that a
 * retry cannot change: an unsupported format, a malware hit, a file over the
 * archive's limit, a capture session already finalised, a permission the
 * person does not have, and a session or item that no longer exists.
 */
export const BLOCKED_REASONS = [
  "unsupported_type",
  "infected",
  "too_large_for_server",
  "session_closed",
  "not_permitted",
  "not_found",
  "rejected",
  // Not a refusal of THIS page. The first page of its receipt was refused, so
  // there is no item for this one to join — and sending it alone would open a
  // second receipt for half an invoice. Cascaded rather than left waiting,
  // because a page whose first page will never arrive would otherwise sit in
  // the queue forever with nothing to say about why.
  "first_page_blocked",
] as const;

export type BlockedReason = (typeof BLOCKED_REASONS)[number];

/** Why the queue would not take a capture in the first place (MOB-009's cap). */
export type RefusalReason = "cap_exceeded" | "larger_than_cap";

/**
 * MOB-009's three purge triggers, named rather than passed as a string so a
 * caller cannot invent a fourth that means "nearly a logout".
 *
 *   logout       the person signed out.
 *   role_change  their grants changed. The queued receipts were captured under
 *                an authority that may no longer exist, and uploading them
 *                afterwards would submit claims on a permission the person no
 *                longer holds (IAM-030: decisions are evaluated against
 *                CURRENT state).
 *   remote_wipe  the device was wiped by an administrator.
 */
export type PurgeReason = "logout" | "role_change" | "remote_wipe";

/**
 * A capture handed to the queue.
 *
 * Every tenant identifier the upload will need is captured HERE, at the
 * moment the shutter closes, and travels with the record. Nothing in the
 * uploader reads an "currently open administration": a receipt photographed
 * for one client and uploaded an hour later, after the person has switched to
 * another, must still be posted to the client it was photographed for. That is
 * CLAUDE.md's first rule seen from the client side — the request carries its
 * tenant context, and the context is not re-derived from ambient state.
 */
export interface NewCapture {
  readonly organizationId: string;
  readonly administrationId: string;
  readonly fiscalYearId: string;
  /** Who captured it. A queue holding another user's work is a purge that did not happen. */
  readonly userId: string;
  /** FR-EXP-001a's sitting. */
  readonly sessionId: string;
  /**
   * Which RECEIPT this page belongs to, as a client-local id.
   *
   * ADR-031 §3 distinguishes FR-EXP-001's multi-page from FR-EXP-001a's batch
   * with `?item=<uuid>`, and that uuid is allocated by the SERVER when a
   * receipt's first page lands. A queue cannot know it: offline, page two of an
   * invoice is photographed long before page one has been anywhere.
   *
   * So the capture screen groups pages under a reference it mints itself, and
   * the queue resolves that to the server's item id when the first page is
   * delivered (see `StoredCapture.resolvedItemId`). Without this, every page
   * captured offline would arrive as a NEW receipt — a three-page invoice
   * claimed three times, silently, which is the exact failure ADR-031's schema
   * was shaped to make impossible.
   */
  readonly receiptRef: string;
  /**
   * 0 for the page that opens a receipt, 1.. for each further original.
   *
   * Explicit rather than derived from queue order: once page 0 is delivered its
   * record is gone, and "the earliest record with this reference" would then
   * name page 1 and open a second receipt.
   */
  readonly pageIndex: number;
  readonly source: CaptureSource;
  readonly filename: string | null;
  readonly contentType: string;
  /**
   * The expense category the person picked for this receipt, as the API's
   * category key. Opaque here — the queue neither validates nor interprets it
   * (the server owns the list). Only a receipt's first page carries it into
   * the request: the expense is created by that page, and later pages join it.
   */
  readonly category?: string | null;
  readonly image: Bytes;
}

/**
 * The metadata sealed alongside the image.
 *
 * Everything that identifies the receipt, the client it belongs to and the
 * person who took it is inside the envelope — MOB-009 is about what is written
 * to device storage, and an administration id in the clear is a record of
 * which client this device has been working for.
 */
export interface SealedPayload extends Omit<NewCapture, "image"> {
  /**
   * NFR-032. Minted once, when the capture is queued, and reused on every
   * attempt — that is the whole point. A key regenerated per attempt would
   * double-post exactly the receipt whose first response was lost on a
   * flapping connection, which is the case this queue exists for.
   */
  readonly idempotencyKey: string;
  readonly capturedAt: string;
}

/**
 * What the device actually holds.
 *
 * The cleartext fields are the ones the queue must read WITHOUT the key: to
 * show the queue, to pick what is due, and to enforce the cap. None of them is
 * financial data — a state, a byte count and two timestamps say that some
 * number of documents are waiting, and nothing about what is in them.
 *
 * That split has a second payoff. A phone that has auto-locked (MOB-008) can
 * still render "4 receipts waiting to upload" without the key being available,
 * so the queue is visible before the person unlocks rather than after.
 */
export interface StoredCapture {
  readonly id: string;
  readonly state: QueueState;
  /**
   * Cleartext, because the queue has to group and order by it without the key
   * — and because it is a client-local opaque id that says nothing about the
   * client, the person or the money. See the note on `resolvedItemId`.
   */
  readonly receiptRef: string;
  readonly pageIndex: number;
  /**
   * The server's item id for this receipt, once its first page has been
   * delivered. Null until then, and null forever on a page-0 record.
   *
   * Cleartext for one reason: it is written AFTER the envelope was sealed, and
   * re-sealing every waiting sibling would mean decrypting each one to change a
   * single field. The tamper case it opens is bounded — someone with write
   * access to the device store could attach a page to the wrong receipt, but
   * only within the same capture session, because the server verifies the item
   * belongs to the session it was sent with (ADR-031: "Checked rather than
   * trusted"). Cross-tenant is not reachable from here.
   */
  readonly resolvedItemId: string | null;
  /** Ciphertext length: what this record costs the device, which is what MOB-009 caps. */
  readonly bytes: number;
  readonly capturedAt: string;
  readonly attempts: number;
  /** Epoch millis before which the uploader must not try again. */
  readonly nextAttemptAt: number | null;
  readonly lastAttemptAt: number | null;
  readonly blockedReason: BlockedReason | null;
  readonly sealed: SealedBlob;
}

/** AES-GCM output. `iv` is per-record and never reused. */
export interface SealedBlob {
  readonly iv: Bytes;
  readonly ciphertext: Bytes;
}

export type EnqueueResult =
  | { readonly accepted: true; readonly id: string }
  | { readonly accepted: false; readonly reason: RefusalReason; readonly capBytes: number };

/**
 * What a queue display renders — MOB-003's "visible queue state".
 *
 * Derived entirely from cleartext fields, so producing it needs no key.
 */
export interface QueueSnapshot {
  readonly items: readonly QueueItemView[];
  readonly queued: number;
  readonly uploading: number;
  readonly blocked: number;
  /**
   * Deliveries since this queue object was constructed. In memory only: it is
   * a progress indicator for the drain the person is watching, not a record.
   */
  readonly delivered: number;
  readonly usedBytes: number;
  readonly capBytes: number;
  /** True while the uploader believes there is no connectivity. */
  readonly offline: boolean;
}

export interface QueueItemView {
  readonly id: string;
  readonly state: QueueState;
  readonly receiptRef: string;
  readonly pageIndex: number;
  readonly bytes: number;
  readonly capturedAt: string;
  readonly attempts: number;
  readonly nextAttemptAt: number | null;
  readonly blockedReason: BlockedReason | null;
}

/**
 * MOB-009's cap, as a byte budget over UNDELIVERED work.
 *
 * 200 MiB is roughly a fortnight of a heavy expense week at the archive's
 * 25 MiB-per-image ceiling, and small enough not to be the reason a phone runs
 * out of space. It is a constructor argument rather than a constant so a
 * tenant policy or a low-storage device can lower it.
 *
 * The queue enforces THIS and nothing else about size. Per-format limits and
 * the accepted format list belong to the document archive (SEC-005, ADR-031
 * §4); re-deriving them here would be the second allowlist ADR-031 refused,
 * and the one that drifted would be the one a receipt was wrongly refused by.
 */
export const DEFAULT_CAP_BYTES = 200 * 1024 * 1024;
