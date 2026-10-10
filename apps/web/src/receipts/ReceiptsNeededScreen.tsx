import { useCallback, useEffect, useState } from "react";
import { CircleCheck, Upload } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { MissingReceiptView } from "@ledgr/shared-types";

import "./Receipts.css";
import { CaptureScreen } from "../capture/CaptureScreen";
import { useQueueSnapshot } from "../capture/CaptureQueueStatus";
import { useSitting } from "../capture/useSitting";
import { useResource } from "../firm/useResource";
import { useServices } from "../session/ServicesProvider";
import { useAdministration } from "../session/SessionProvider";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { Button, useMoney } from "../ui";

/**
 * `/receipts-needed` - the bank payments that still need a receipt (the link in the chasing mail,
 * docs/firm-home/contract-wave2.md decision 5). Each line's "Upload receipt" opens the EXISTING
 * capture flow (`CaptureScreen` on a `useSitting` sitting, exactly as Purchases embeds it): there
 * is one uploader in this app, and the server's matcher pairs what arrives with the payment.
 *
 * The capture sitting opens only when somebody presses "Upload receipt" - mounting this screen
 * reads the list and posts nothing.
 */
export function ReceiptsNeededScreen() {
  const { t, date } = useI18n();
  const money = useMoney();
  const { administration } = useAdministration();
  const { receipts, queue } = useServices();
  const load = useCallback(
    () => receipts.listMissing(administration.id),
    [receipts, administration.id],
  );
  const list = useResource(load);
  const [uploadingFor, setUploadingFor] = useState<MissingReceiptView | null>(null);

  // A delivered upload may take a line off this list; re-read it in place (as Purchases does).
  const delivered = useQueueSnapshot(queue)?.delivered ?? 0;
  const { reload } = list;
  useEffect(() => {
    if (delivered > 0) reload();
  }, [delivered, reload]);

  const data = list.data;
  const payee = (line: MissingReceiptView) =>
    line.counterparty !== null && line.counterparty.trim() !== ""
      ? line.counterparty
      : t("client.receipts.unknown_counterparty");

  return (
    <section
      className="screen receipts"
      aria-label={t("client.receipts.title")}
      data-testid="receipts-needed"
    >
      <PageHeader
        title={t("client.receipts.title")}
        {...(data !== null && data.count > 0
          ? { context: t("client.receipts.context", { count: data.count }) }
          : {})}
      />

      {uploadingFor !== null ? (
        <ReceiptCapture
          title={t("client.receipts.capture_title", {
            counterparty: payee(uploadingFor),
            amount: money(uploadingFor.amount),
            date: date(uploadingFor.booking_date),
          })}
          onClose={() => {
            setUploadingFor(null);
            reload();
          }}
        />
      ) : null}

      {data === null && list.loading ? (
        <LoadingSkeleton rows={4} />
      ) : data === null ? (
        <ErrorState message={list.error ?? ""} onRetry={reload} />
      ) : data.items.length === 0 ? (
        <EmptyState
          icon={<CircleCheck size={32} aria-hidden="true" />}
          title={t("client.receipts.empty_title")}
          body={t("client.receipts.empty_body")}
          testId="receipts-empty"
        />
      ) : (
        <>
          <p className="receipts__intro">{t("client.receipts.intro")}</p>
          <ul className="receipts__list" data-testid="receipts-list">
            {data.items.map((line) => (
              <li
                key={line.bank_transaction_id}
                className="receipts__row"
                data-testid={`receipts-line-${line.bank_transaction_id}`}
              >
                <span className="receipts__main">
                  <span className="receipts__payee">{payee(line)}</span>
                  <span className="receipts__muted">
                    <span className="ledgr-visually-hidden">{t("client.receipts.col.date")} </span>
                    {date(line.booking_date)}
                    {line.description !== null && line.description !== "" ? (
                      <> · {line.description}</>
                    ) : null}
                  </span>
                </span>
                <span className="receipts__amount ui-amount">
                  <span className="ledgr-visually-hidden">{t("client.receipts.col.amount")} </span>
                  {money(line.amount)}
                </span>
                <span className="chip chip--caution receipts__status">
                  {t("client.receipts.status")}
                </span>
                <Button
                  size="sm"
                  className="receipts__upload"
                  onClick={() => setUploadingFor(line)}
                  aria-label={t("client.receipts.upload_for", {
                    counterparty: payee(line),
                    amount: money(line.amount),
                    date: date(line.booking_date),
                  })}
                  data-testid={`receipts-upload-${line.bank_transaction_id}`}
                >
                  <Upload size={16} aria-hidden="true" />
                  <span>{t("client.receipts.upload")}</span>
                </Button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/** The existing capture intake, embedded - mounted only after "Upload receipt" was pressed. */
function ReceiptCapture({ title, onClose }: { title: string; onClose: () => void }) {
  const { t } = useI18n();
  const { sittingContext } = useAdministration();
  const { capture, queue, decode } = useServices();
  const sitting = useSitting({ context: sittingContext, queue, api: capture });
  return (
    <section className="panel receipts__capture" aria-label={title} data-testid="receipts-capture">
      <div className="receipts__capture-head">
        <h2 className="receipts__capture-title">{title}</h2>
        <Button size="sm" onClick={onClose} data-testid="receipts-capture-close">
          {t("client.receipts.capture_close")}
        </Button>
      </div>
      <p className="receipts__muted">{t("client.receipts.capture_hint")}</p>
      <CaptureScreen sitting={sitting} queue={queue} decode={decode} variant="embedded" />
    </section>
  );
}
