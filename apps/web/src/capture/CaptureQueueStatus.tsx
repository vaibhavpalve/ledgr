import { useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type {
  BlockedReason,
  CaptureQueue,
  QueueItemView,
  QueueSnapshot,
} from "@ledgr/offline-queue";

/**
 * MOB-003's "with visible queue state" — FR-EXP-001f, MOB-003, MOB-009.
 *
 * --- What "visible" is taken to mean ---
 *
 * Not a spinner. A person who has just photographed six receipts in a car park
 * has one question — did those six survive? — and a queue that answers it only
 * by eventually going quiet is indistinguishable from one that lost them. So
 * the panel always states a count, names the state of every capture
 * individually, and says what it is waiting for:
 *
 *   offline          why nothing is moving, and that it will move on its own
 *   waiting / done   how many are left, how many have gone
 *   needs attention  which ones, WHY, and the two things to do about them
 *   storage          how much of MOB-009's cap is spent
 *
 * --- Blocked captures name a reason and offer both actions ---
 *
 * A capture the server refused permanently is the one case where the queue
 * cannot proceed alone. Showing it as "failed" and nothing else leaves the
 * person with a photograph they cannot use and no idea why, so each carries
 * the sentence for its reason, a retry (grants change; fiscal years open) and
 * a discard that says plainly the photograph goes with it.
 *
 * --- The panel renders without the encryption key ---
 *
 * Everything here comes from `QueueSnapshot`, which `CaptureQueue` derives
 * from cleartext fields alone (MOB-009 keeps the metadata and the image
 * sealed). A device that has auto-locked (MOB-008) can therefore still show
 * how much work is waiting, which is the answer somebody wants before they
 * unlock rather than after.
 */
export function CaptureQueueStatus({
  queue,
  onRetry,
}: {
  queue: CaptureQueue;
  /**
   * Ask the uploader to drain now. Optional: the queue displays perfectly well
   * without one, and a panel that could not be rendered without a transport
   * would be untestable and unusable on a screen that has no uploader running.
   */
  onRetry?: () => void;
}) {
  const snapshot = useQueueSnapshot(queue);
  const { t, date, number } = useI18n();

  if (snapshot === null) return null;

  const { items, queued, uploading, blocked, delivered, usedBytes, capBytes, offline } = snapshot;

  return (
    <section className="capture-queue" aria-label={t("capture.queue.title")}>
      <h2>{t("capture.queue.title")}</h2>

      {offline ? (
        <p className="capture-queue__offline" role="status" data-testid="capture-queue-offline">
          {t("capture.queue.offline")}
        </p>
      ) : null}

      {/*
        One live region for the counts, so a screen reader hears the queue
        shrink rather than having to go looking. `polite`, not `assertive`:
        this is progress, and it must not interrupt what somebody is typing
        into the expense form beside it.
      */}
      <p role="status" aria-live="polite" data-testid="capture-queue-summary">
        {items.length === 0
          ? t("capture.queue.empty")
          : [
              queued > 0 ? t("capture.queue.waiting", { count: queued }) : null,
              uploading > 0 ? t("capture.queue.uploading_now", { count: uploading }) : null,
              blocked > 0 ? t("capture.queue.blocked_count", { count: blocked }) : null,
            ]
              .filter((line): line is string => line !== null)
              .join(" · ")}
      </p>

      {delivered > 0 ? (
        <p data-testid="capture-queue-delivered">
          {t("capture.queue.delivered_count", { count: delivered })}
        </p>
      ) : null}

      <p className="capture-queue__storage" data-testid="capture-queue-storage">
        {t("capture.queue.storage", {
          used: number(megabytes(usedBytes), { scale: 1 }),
          total: number(megabytes(capBytes), { scale: 1 }),
        })}
      </p>

      <ul aria-label={t("capture.queue.list_label")} data-testid="capture-queue-list">
        {items.map((item, index) => {
          // `capturedAt` is a full instant; the shared date formatter takes a
          // calendar date and refuses anything else (FR-LOC-002 — it renders
          // in the administration's locale, and there is no time format in the
          // catalogue to render the rest with). The position is what
          // distinguishes two receipts photographed on the same day.
          const captured = date(item.capturedAt.slice(0, 10), "long");
          const position = index + 1;
          return (
            <li
              key={item.id}
              data-testid="capture-queue-item"
              data-state={item.state}
              data-blocked-reason={item.blockedReason ?? undefined}
            >
              <span className="capture-queue__item-name">
                {t("capture.queue.item_label", { position, captured })}
              </span>
              <span className="capture-queue__item-state">{t(stateKey(item))}</span>

              {/*
                Shown only once something has actually failed. A "0 attempts"
                on every freshly queued receipt would be noise, and noise is
                what stops the line being read on the day it matters.
              */}
              {item.attempts > 0 && item.state !== "uploading" ? (
                <span className="capture-queue__item-attempts">
                  {t("capture.queue.attempts", { count: item.attempts })}
                </span>
              ) : null}

              {item.blockedReason !== null ? (
                <>
                  <span className="capture-queue__item-reason">
                    {t(reasonKey(item.blockedReason))}
                  </span>
                  <button
                    type="button"
                    aria-label={t("capture.queue.retry_label", { position })}
                    data-testid="capture-queue-retry"
                    onClick={() => {
                      void queue.unblock(item.id).then(() => onRetry?.());
                    }}
                  >
                    {t("capture.queue.retry")}
                  </button>
                  <button
                    type="button"
                    aria-label={t("capture.queue.discard_label", { position })}
                    data-testid="capture-queue-discard"
                    onClick={() => {
                      void queue.discard(item.id);
                    }}
                  >
                    {t("capture.queue.discard")}
                  </button>
                </>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * MOB-009's warning, for a sign-out flow to render before it offers the
 * button.
 *
 * A separate component rather than a branch inside the panel above, because it
 * belongs on a different screen: the queue panel is where somebody watches
 * work leave, and this is the sentence that has to appear where they are about
 * to destroy it.
 */
export function CaptureQueuePurgeWarning({ pending }: { pending: number }) {
  const { t } = useI18n();
  if (pending === 0) return null;
  return (
    <p role="alert" data-testid="capture-queue-purge-warning">
      {t("capture.queue.purge_warning", { count: pending })}
    </p>
  );
}

/**
 * The queue as it currently is, kept current.
 *
 * `null` until the first read resolves — reading IndexedDB is asynchronous, and
 * rendering "0 receipts waiting" in the meantime would state, briefly and
 * confidently, the one thing somebody is afraid of.
 */
export function useQueueSnapshot(queue: CaptureQueue): QueueSnapshot | null {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null);

  useEffect(() => {
    let live = true;
    const unsubscribe = queue.subscribe((next) => {
      if (live) setSnapshot(next);
    });
    void queue.snapshot().then((next) => {
      if (live) setSnapshot(next);
    });
    return () => {
      live = false;
      unsubscribe();
    };
  }, [queue]);

  return snapshot;
}

function stateKey(item: QueueItemView): string {
  return `capture.queue.state.${item.state}`;
}

function reasonKey(reason: BlockedReason): string {
  return `capture.queue.reason.${reason}`;
}

/**
 * Bytes as megabytes, as a decimal STRING for `number()` to format.
 *
 * A string because that is what the shared formatter takes, and it takes a
 * string for NFR-031's reason: every figure in this product crosses a boundary
 * as text so that nothing has been through a double on the way. A byte count
 * is not money and would survive the trip — but having one exception is how
 * the rule stops being one.
 */
function megabytes(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(1);
}
