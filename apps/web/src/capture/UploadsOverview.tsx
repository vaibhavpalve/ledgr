import { useI18n } from "@ledgr/i18n";
import type { BlockedReason, CaptureQueue, QueueItemView } from "@ledgr/offline-queue";
import { CircleAlert, CircleCheck, FileImage, FileText, LoaderCircle, WifiOff } from "lucide-react";

import { useQueueSnapshot } from "./CaptureQueueStatus";
import { expensesThisSittingWouldCreate, type LocalReceipt, type Sitting } from "./useSitting";

/**
 * FR-EXP-001a's "review list before posting" and MOB-003's "visible queue
 * state", as one overview instead of two lists of the same receipts.
 *
 * --- What each row is built from ---
 *
 * The receipt itself (its file name, category and page count) is what THIS
 * sitting captured - it is known immediately and offline, before the server has
 * seen any of it. Where it has got to is read off the queue:
 *
 *     records in the queue, one uploading   -> Uploading
 *     records in the queue, none uploading  -> Waiting to upload
 *     a record the server refused           -> Needs attention, with the reason
 *     no records left                       -> Uploaded
 *
 * "No records left" is how a delivered capture looks - the queue deletes what it
 * has delivered rather than marking it (see `@ledgr/offline-queue`'s model), so
 * the absence IS the signal. It is why a discarded receipt has to be forgotten by
 * the sitting as well (`Sitting.forget`): its records are gone too, and would
 * otherwise read as uploaded.
 */
export function UploadsOverview({ sitting, queue }: { sitting: Sitting; queue: CaptureQueue }) {
  const { t, number } = useI18n();
  const snapshot = useQueueSnapshot(queue);

  const items = snapshot?.items ?? [];
  const rows = sitting.receipts.map((receipt) => ({
    receipt,
    progress: progressOf(
      items.filter((item) => item.receiptRef === receipt.ref),
      snapshot !== null,
    ),
  }));
  const done = rows.filter((row) => row.progress.status === "done").length;

  return (
    <section className="uploads" aria-label={t("capture.uploads.title")}>
      <header className="uploads__header">
        <h2>{t("capture.uploads.title")}</h2>
        {rows.length > 0 ? (
          <p
            className="uploads__summary"
            role="status"
            aria-live="polite"
            data-testid="capture-uploads-summary"
          >
            {t("capture.uploads.summary", { done, total: rows.length })}
          </p>
        ) : null}
      </header>

      {snapshot?.offline ? (
        <p className="uploads__offline" role="status" data-testid="capture-queue-offline">
          <WifiOff size={16} strokeWidth={1.75} aria-hidden="true" />
          {t("capture.queue.offline")}
        </p>
      ) : null}

      {rows.length === 0 ? (
        <p className="uploads__empty" data-testid="capture-review-empty">
          {t("capture.uploads.empty")}
        </p>
      ) : (
        <>
          <progress
            className="uploads__bar"
            max={rows.length}
            value={done}
            aria-label={t("capture.uploads.summary", { done, total: rows.length })}
          />
          <p className="uploads__count" data-testid="capture-expenses-to-create">
            {t("capture.screen.expenses_to_create", {
              count: expensesThisSittingWouldCreate(sitting.receipts),
            })}
          </p>
          <ul
            className="uploads__list"
            aria-label={t("capture.uploads.list_label")}
            data-testid="capture-review-list"
          >
            {rows.map(({ receipt, progress }, index) => (
              <UploadRow
                key={receipt.ref}
                receipt={receipt}
                position={index + 1}
                progress={progress}
                onRetry={() => {
                  for (const item of progress.blockedItems) void queue.unblock(item.id);
                }}
                onDiscard={() => {
                  void (async () => {
                    for (const item of progress.items) await queue.discard(item.id);
                    sitting.forget(receipt.ref);
                  })();
                }}
              />
            ))}
          </ul>
        </>
      )}

      {snapshot !== null && snapshot.usedBytes > 0 ? (
        <p className="uploads__storage" data-testid="capture-queue-storage">
          {t("capture.queue.storage", {
            used: number(megabytes(snapshot.usedBytes), { scale: 1 }),
            total: number(megabytes(snapshot.capBytes), { scale: 1 }),
          })}
        </p>
      ) : null}
    </section>
  );
}

function UploadRow({
  receipt,
  position,
  progress,
  onRetry,
  onDiscard,
}: {
  receipt: LocalReceipt;
  position: number;
  progress: Progress;
  onRetry: () => void;
  onDiscard: () => void;
}) {
  const { t } = useI18n();
  const first = receipt.pages[0];
  const isPdf = first?.contentType === "application/pdf";
  const Glyph = isPdf ? FileText : FileImage;

  return (
    <li
      className="uploads__row"
      data-testid="capture-review-item"
      data-receipt-ref={receipt.ref}
      data-status={progress.status}
    >
      <span className="uploads__file" aria-hidden="true">
        <Glyph size={20} strokeWidth={1.5} />
      </span>

      <div className="uploads__body">
        <p className="uploads__name">
          {first?.filename ? first.filename : t("capture.review.receipt", { position })}
        </p>
        <p className="uploads__meta">
          <span
            className={
              receipt.category === null
                ? "uploads__category uploads__category--none"
                : "uploads__category"
            }
            data-testid="capture-review-category"
          >
            {receipt.category === null
              ? t("capture.uploads.no_category")
              : t(`capture.category.${receipt.category}`)}
          </span>
          <span data-testid="capture-review-pages">
            {t("capture.review.pages", { count: receipt.pages.length })}
          </span>
        </p>
        {progress.reason !== null ? (
          <p className="uploads__reason" data-testid="capture-review-reason">
            {t(`capture.queue.reason.${progress.reason}`)}
          </p>
        ) : null}
      </div>

      <div className="uploads__aside">
        <span className={`uploads__status uploads__status--${progress.status}`}>
          <StatusGlyph status={progress.status} />
          {t(`capture.uploads.status.${progress.status}`)}
        </span>
        {progress.status === "blocked" ? (
          <span className="uploads__actions">
            <button
              type="button"
              className="uploads__action"
              aria-label={t("capture.queue.retry_label", { position })}
              data-testid="capture-queue-retry"
              onClick={onRetry}
            >
              {t("capture.queue.retry")}
            </button>
            <button
              type="button"
              className="uploads__action"
              aria-label={t("capture.queue.discard_label", { position })}
              data-testid="capture-queue-discard"
              onClick={onDiscard}
            >
              {t("capture.queue.discard")}
            </button>
          </span>
        ) : null}
      </div>
    </li>
  );
}

function StatusGlyph({ status }: { status: UploadStatus }) {
  if (status === "done") return <CircleCheck size={14} strokeWidth={2} aria-hidden="true" />;
  if (status === "blocked") return <CircleAlert size={14} strokeWidth={2} aria-hidden="true" />;
  if (status === "uploading") {
    return <LoaderCircle className="uploads__spin" size={14} strokeWidth={2} aria-hidden="true" />;
  }
  return <span className="uploads__dot" aria-hidden="true" />;
}

type UploadStatus = "queued" | "uploading" | "done" | "blocked";

interface Progress {
  readonly status: UploadStatus;
  /** Every queued record of this receipt - one per page still to send. */
  readonly items: readonly QueueItemView[];
  readonly blockedItems: readonly QueueItemView[];
  /** Why the first blocked page was refused, in the catalogue's own vocabulary. */
  readonly reason: BlockedReason | null;
}

function progressOf(items: readonly QueueItemView[], queueKnown: boolean): Progress {
  // Until the queue has been read, "no records" would say "uploaded" for a
  // receipt nothing has been confirmed about - the wrong thing to be confident
  // of, briefly, on a screen whose whole job is answering that question.
  if (!queueKnown) return { status: "queued", items, blockedItems: [], reason: null };

  const blockedItems = items.filter((item) => item.state === "blocked");
  if (blockedItems.length > 0) {
    // A receipt's first page is the one whose refusal is the real reason; the
    // rest are cascaded from it (`first_page_blocked`).
    const lead = blockedItems.find((item) => item.pageIndex === 0) ?? blockedItems[0]!;
    return { status: "blocked", items, blockedItems, reason: lead.blockedReason };
  }
  if (items.some((item) => item.state === "uploading")) {
    return { status: "uploading", items, blockedItems, reason: null };
  }
  if (items.length > 0) return { status: "queued", items, blockedItems, reason: null };
  return { status: "done", items, blockedItems, reason: null };
}

/** Bytes as a decimal string for `number()` - see `CaptureQueueStatus`. */
function megabytes(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(1);
}
