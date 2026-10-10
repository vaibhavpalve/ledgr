import { useCallback, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type {
  FirmChaseBlockedReason,
  FirmChasePreviewItem,
  FirmChaseSendResultView,
  FirmClientRef,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { ErrorState, LoadingSkeleton } from "../shell/ScreenState";
import { Badge, Button } from "../ui";
import type { FirmApiShape } from "./api";
import { FailureList, FirmDialog } from "./FirmDialogs";
import { localDateOf, useResource } from "./useResource";

const KNOWN_REASONS: ReadonlySet<string> = new Set<FirmChaseBlockedReason>([
  "nothing_missing",
  "chased_recently",
  "no_recipient",
]);

/**
 * "Request missing receipts" from the bulk bar (ADR-114, FR-NTF-001/004): first a preview of
 * what would go to each selected client - how many receipts are missing, when they were last
 * chased, and in words why a client would be skipped - and only then a send. The mail itself
 * names no amounts or counterparties; the server enforces the 24-hour gap and says who it skipped.
 */
export function ChaseDialog({
  api,
  clients,
  onClose,
  onDone,
}: {
  api: FirmApiShape;
  clients: readonly FirmClientRef[];
  onClose: () => void;
  /** After a send that reached anyone, so the list can show "Chased today". */
  onDone: () => unknown;
}) {
  const { t, date } = useI18n();
  const ids = clients.map((client) => client.administration_id);
  const idKey = ids.join(",");
  const load = useCallback(
    () => api.previewChase(idKey === "" ? [] : idKey.split(",")),
    [api, idKey],
  );
  const preview = useResource(load);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [result, setResult] = useState<FirmChaseSendResultView | null>(null);

  const items = preview.data?.items ?? [];
  const sendable = items.filter((item) => item.blocked_reason === null).length;
  const nameOf = (id: string) =>
    items.find((item) => item.administration_id === id)?.display_name ??
    clients.find((client) => client.administration_id === id)?.display_name ??
    id;
  const reasonText = (reason: string) =>
    KNOWN_REASONS.has(reason) ? t(`client.todo.chase.reason.${reason}`) : reason;

  const send = async () => {
    setBusy(true);
    setProblem(null);
    try {
      // Every selected client goes to the server, which decides against current state (the
      // preview may be minutes old) and lists whom it skipped.
      const answer = await api.sendChase(ids);
      setResult(answer);
      if (answer.sent > 0) void onDone();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <FirmDialog
      title={t("client.todo.chase.title")}
      subtitle={t("client.todo.chase.subtitle", { count: clients.length })}
      onClose={onClose}
      testId="firm-chase-dialog"
    >
      <p className="firm-muted">{t("client.todo.chase.intro")}</p>

      {preview.data === null && preview.loading ? (
        <LoadingSkeleton rows={3} testId="firm-chase-loading" />
      ) : preview.data === null ? (
        <ErrorState
          message={preview.error ?? t("client.todo.panel_error")}
          onRetry={preview.reload}
          testId="firm-chase-error"
        />
      ) : (
        <ul className="firm-chase__list" data-testid="firm-chase-preview">
          {items.map((item) => (
            <PreviewLine
              key={item.administration_id}
              item={item}
              reasonText={reasonText}
              lastChased={(at) => {
                const day = localDateOf(at);
                return day === null ? at : date(day);
              }}
            />
          ))}
        </ul>
      )}

      {result !== null ? (
        <p role="status" data-testid="firm-chase-result">
          {t("client.todo.chase.sent", { count: result.sent })}
        </p>
      ) : null}
      {result !== null && result.skipped.length > 0 ? (
        <FailureList
          heading={t("client.todo.chase.skipped", { count: result.skipped.length })}
          items={result.skipped.map((skip) => ({
            key: skip.administration_id,
            label: nameOf(skip.administration_id),
            reason: reasonText(skip.reason),
          }))}
          testId="firm-chase-skipped"
        />
      ) : null}
      {problem !== null ? (
        <p className="field-error" role="alert">
          {problem}
        </p>
      ) : null}

      <div className="dialog__actions">
        <Button onClick={onClose} data-testid="firm-chase-close">
          {result !== null ? t("common.action.close") : t("common.action.cancel")}
        </Button>
        {result === null ? (
          <Button
            variant="primary"
            disabled={busy || preview.data === null || sendable === 0}
            onClick={() => void send()}
            data-testid="firm-chase-send"
          >
            {busy ? t("client.todo.working") : t("client.todo.chase.send", { count: sendable })}
          </Button>
        ) : null}
      </div>
    </FirmDialog>
  );
}

function PreviewLine({
  item,
  reasonText,
  lastChased,
}: {
  item: FirmChasePreviewItem;
  reasonText: (reason: string) => string;
  lastChased: (at: string) => string;
}) {
  const { t } = useI18n();
  const blocked = item.blocked_reason;
  return (
    <li className="firm-chase__item" data-testid={`firm-chase-item-${item.administration_id}`}>
      <span className="firm-chase__name">{item.display_name}</span>
      <span className="firm-muted firm-chase__facts">
        {t("client.todo.chase.missing", { count: item.missing_count })}
        {" · "}
        {item.last_chased_at !== null
          ? t("client.todo.chase.last_chased", { date: lastChased(item.last_chased_at) })
          : t("client.todo.chase.never_chased")}
      </span>
      <span className="firm-chase__status">
        {blocked === null ? (
          <Badge variant="booked">{t("client.todo.chase.will_send")}</Badge>
        ) : (
          <Badge variant="neutral">
            {t("client.todo.chase.will_skip", { reason: reasonText(blocked) })}
          </Badge>
        )}
      </span>
    </li>
  );
}
