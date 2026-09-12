/**
 * The offline capture queue — FR-EXP-001f, MOB-003, MOB-009.
 *
 * Platform-free by construction: see model.ts for what that buys and ports.ts
 * for what a platform has to supply. The web app's adapters live in
 * apps/web/src/capture.
 */

export { CaptureQueue, type CaptureQueueOptions } from "./queue";
export { QueueUploader, type QueueUploaderOptions } from "./uploader";
export { CorruptEnvelope, frame, unframe } from "./envelope";
export { admit, backoffMs, due, nextDueAt, usedBytes, view } from "./policy";
export { buildRequest, classify, type Classification } from "./request";
export {
  BLOCKED_REASONS,
  DEFAULT_CAP_BYTES,
  QUEUE_STATES,
  type BlockedReason,
  type Bytes,
  type CaptureSource,
  type EnqueueResult,
  type NewCapture,
  type PurgeReason,
  type QueueItemView,
  type QueueSnapshot,
  type QueueState,
  type RefusalReason,
  type SealedBlob,
  type SealedPayload,
  type StoredCapture,
} from "./model";
export type {
  Cancel,
  CaptureRequest,
  Clock,
  Connectivity,
  PurgeEvent,
  QueueCipher,
  QueueStore,
  Scheduler,
  Transport,
  TransportResponse,
  Wakeup,
} from "./ports";
