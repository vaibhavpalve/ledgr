import { useCallback, useMemo, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";
import { CircleAlert, X } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { CaptureDuplicate, CaptureQueue } from "@ledgr/offline-queue";

/**
 * ADR-116: "this looks like an invoice you already have", said at the upload.
 *
 * FR-EXP-001g warns about a duplicate; this is WHERE. It used to be a list
 * inside the form of the invoice, found by whoever opened it afterwards and, for
 * an invoice uploaded four times, repeated four times. The answer is now
 * available when the file lands - in the upload's own response - so the person
 * is told while they are still looking at the upload.
 *
 * It warns and never refuses. The file is stored and the draft exists either
 * way (a refused upload is a lost document, and two identical invoices can be
 * legitimate); what a notice does is name the invoice it resembles and link to
 * both. A notice stays until it is dismissed, because a duplicate announced for
 * three seconds is a duplicate nobody saw.
 */
export interface UploadNotice {
  /** The receipt's client-local reference, which is what makes a notice one per upload. */
  readonly receiptRef: string;
  /** The client it was uploaded into: a screen showing another one does not show it. */
  readonly administrationId: string;
  /** The draft the upload became; the link to "this upload". */
  readonly expenseId: string | null;
  /** The file's name as uploaded, where there was one. */
  readonly filename: string | null;
  readonly duplicate: CaptureDuplicate;
}

/**
 * Notices outlive the screen that happened to be open when the upload landed.
 *
 * Held per queue, outside React: a person who uploads, opens the invoice and
 * comes back to the list is owed the notice still being there, and component
 * state would have gone with the list. The store subscribes to
 * `CaptureQueue.onDelivered` once, for as long as the queue lives - which is
 * the session's, and is purged with it (MOB-009).
 */
class NoticeStore {
  private notices: readonly UploadNotice[] = [];
  private readonly listeners = new Set<() => void>();

  constructor(queue: CaptureQueue) {
    queue.onDelivered((event) => {
      if (event.duplicate === null) return;
      this.notices = [
        ...this.notices.filter((notice) => notice.receiptRef !== event.receiptRef),
        {
          receiptRef: event.receiptRef,
          administrationId: event.administrationId,
          expenseId: event.expenseId,
          filename: event.filename,
          duplicate: event.duplicate,
        },
      ];
      this.emit();
    });
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  readonly snapshot = (): readonly UploadNotice[] => this.notices;

  readonly dismiss = (receiptRef: string): void => {
    this.notices = this.notices.filter((notice) => notice.receiptRef !== receiptRef);
    this.emit();
  };

  private emit(): void {
    for (const listener of this.listeners) listener();
  }
}

const stores = new WeakMap<CaptureQueue, NoticeStore>();

function storeFor(queue: CaptureQueue): NoticeStore {
  let store = stores.get(queue);
  if (store === undefined) {
    store = new NoticeStore(queue);
    stores.set(queue, store);
  }
  return store;
}

/**
 * The duplicate notices for one client's uploads, as the queue delivered them.
 *
 * Reads `CaptureQueue.onDelivered` rather than a snapshot, because "this one was
 * a duplicate" is an event: a snapshot only says that a number went up.
 */
export function useUploadNotices(
  queue: CaptureQueue,
  administrationId: string,
): { readonly notices: readonly UploadNotice[]; dismiss(receiptRef: string): void } {
  const store = storeFor(queue);
  const all = useSyncExternalStore(store.subscribe, store.snapshot);
  const notices = useMemo(
    () => all.filter((notice) => notice.administrationId === administrationId),
    [all, administrationId],
  );
  const dismiss = useCallback((receiptRef: string) => store.dismiss(receiptRef), [store]);
  return { notices, dismiss };
}

export function DuplicateUploadNotices({
  notices,
  onDismiss,
}: {
  notices: readonly UploadNotice[];
  onDismiss: (receiptRef: string) => void;
}) {
  const { t } = useI18n();
  if (notices.length === 0) return null;

  return (
    <section
      className="upload-duplicates"
      aria-label={t("capture.duplicate.upload.list_label")}
      data-testid="upload-duplicates"
    >
      {notices.map((notice) => (
        <UploadNoticeCard key={notice.receiptRef} notice={notice} onDismiss={onDismiss} />
      ))}
    </section>
  );
}

function UploadNoticeCard({
  notice,
  onDismiss,
}: {
  notice: UploadNotice;
  onDismiss: (receiptRef: string) => void;
}) {
  const { t, money, date } = useI18n();
  const { duplicate } = notice;
  const file = notice.filename ?? t("capture.duplicate.upload.unnamed_file");

  const existing = t("capture.duplicate.entry", {
    supplier: duplicate.supplier ?? t("capture.details.none"),
    date: duplicate.expenseDate !== null ? date(duplicate.expenseDate) : t("capture.details.none"),
    amount: duplicate.grossAmount !== null ? money(duplicate.grossAmount) : t("capture.details.none"),
  });

  const bodyKey =
    duplicate.match === "same_file"
      ? "capture.duplicate.upload.body.same_file"
      : duplicate.match === "same_invoice"
        ? "capture.duplicate.upload.body.same_invoice"
        : duplicate.invoiceNumberMatch === "missing"
          ? "capture.duplicate.upload.body.same_details_missing"
          : "capture.duplicate.upload.body.same_details_different";

  // A file match and a same-number match are the near-certain ones. Matching
  // details with a different number is quiet: it may well be a second invoice.
  const strong = duplicate.match !== "same_details";
  const others = duplicate.count - 1;

  return (
    <article
      className={
        strong ? "upload-duplicate upload-duplicate--strong" : "upload-duplicate"
      }
      data-testid="upload-duplicate"
      data-match={duplicate.match}
    >
      <CircleAlert
        size={20}
        strokeWidth={1.75}
        aria-hidden="true"
        className="upload-duplicate__icon"
      />

      <div className="upload-duplicate__body">
        <h3>{t(`capture.duplicate.upload.title.${duplicate.match}`)}</h3>
        <p data-testid="upload-duplicate-text">{t(bodyKey, { file, existing })}</p>
        <p className="upload-duplicate__who">
          {duplicate.sameSubmitter
            ? t("capture.duplicate.same_submitter")
            : t("capture.duplicate.other_submitter")}
          {others > 0 ? ` ${t("capture.duplicate.upload.more", { count: others })}` : ""}
        </p>

        <p className="upload-duplicate__actions">
          <Link
            to={`/purchases/${duplicate.expenseId}`}
            className="upload-duplicate__link"
            data-testid="upload-duplicate-open-existing"
          >
            {t("capture.duplicate.upload.open_existing")}
          </Link>
          {notice.expenseId !== null ? (
            <Link
              to={`/purchases/${notice.expenseId}`}
              className="upload-duplicate__link"
              data-testid="upload-duplicate-open-new"
            >
              {t("capture.duplicate.upload.open_new")}
            </Link>
          ) : null}
        </p>
      </div>

      <button
        type="button"
        className="upload-duplicate__dismiss"
        aria-label={t("capture.duplicate.upload.dismiss_label", { file })}
        data-testid="upload-duplicate-dismiss"
        onClick={() => onDismiss(notice.receiptRef)}
      >
        <X size={16} strokeWidth={1.75} aria-hidden="true" />
      </button>
    </article>
  );
}
