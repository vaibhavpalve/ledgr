import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { X } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { FirmAssignResultView, FirmWorklistRowView } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { Button } from "../ui";
import { useModalFocus } from "../useModalFocus";
import type { FirmApiShape } from "./api";
import { addDays, isoDay, useResource } from "./useResource";

/**
 * The firm home's modal frame: the app's own `.dialog` on its backdrop, focus trapped and
 * restored (`useModalFocus`), Escape on the document (a key handler on `role="dialog"` is what
 * jsx-a11y rules out - see CategoryPicker). `drawer` docks it to the right on a wide screen.
 */
export function FirmDialog({
  title,
  subtitle,
  onClose,
  drawer,
  children,
  testId,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  drawer?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useModalFocus(true, ref);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className={`dialog-backdrop${drawer ? " firm-drawer-backdrop" : ""}`}>
      <div
        ref={ref}
        className={`dialog firm-dialog${drawer ? " firm-dialog--drawer" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid={testId}
      >
        <div className="firm-dialog__head">
          <div>
            <h2 id={titleId} className="firm-dialog__title">
              {title}
            </h2>
            {subtitle !== undefined ? <p className="firm-muted">{subtitle}</p> : null}
          </div>
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            aria-label={t("common.action.close")}
            onClick={onClose}
            data-testid="firm-dialog-close"
          >
            <X size={18} aria-hidden="true" />
          </Button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** The server's per-item failures, each with the client's name where we know it. */
export function FailureList({
  heading,
  items,
  testId,
}: {
  heading: string;
  items: readonly { key: string; label: string; reason: string }[];
  testId?: string;
}) {
  return (
    <div role="alert" className="alert alert--attention firm-failures" data-testid={testId}>
      <p className="firm-failures__heading">{heading}</p>
      <ul className="firm-failures__list">
        {items.map((item) => (
          <li key={item.key}>
            <strong>{item.label}</strong>
            {": "}
            {item.reason}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * FR-FRM-004b, partial: assign one or more clients to a colleague (or to nobody). Partial
 * failure is reported per client and the dialog stays open on it, so nothing is silently lost.
 */
export function AssignDialog({
  api,
  rows,
  onClose,
  onDone,
}: {
  api: FirmApiShape;
  rows: readonly Pick<FirmWorklistRowView, "administration_id" | "display_name">[];
  onClose: () => void;
  /** Called after any assignment landed, so the list can refresh. */
  onDone: () => unknown;
}) {
  const { t } = useI18n();
  const loadStaff = useCallback(() => api.listStaff(), [api]);
  const staff = useResource(loadStaff);
  const [userId, setUserId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [result, setResult] = useState<FirmAssignResultView | null>(null);
  const fieldId = useId();

  useEffect(() => {
    if (userId === "" && staff.data !== null && staff.data[0] !== undefined) {
      setUserId(staff.data[0].user_id);
    }
  }, [staff.data, userId]);

  const nameOf = (id: string) =>
    rows.find((row) => row.administration_id === id)?.display_name ?? id;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const answer = await api.assign(
        rows.map((row) => row.administration_id),
        userId === "none" ? null : userId,
      );
      setResult(answer);
      if (answer.assigned > 0) void onDone();
      if (answer.failed.length === 0) onClose();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <FirmDialog
      title={t("client.todo.assign.title", { count: rows.length })}
      onClose={onClose}
      testId="firm-assign-dialog"
    >
      <form className="firm-form" onSubmit={(event) => void submit(event)}>
        {staff.error !== null ? (
          <p className="field-error" role="alert">
            {staff.error}
          </p>
        ) : null}
        <div className="ui-field">
          <label className="ui-label" htmlFor={fieldId}>
            {t("client.todo.assign.to")}
          </label>
          <select
            id={fieldId}
            className="ui-input"
            value={userId}
            disabled={staff.data === null}
            data-testid="firm-assign-user"
            onChange={(event) => setUserId(event.target.value)}
          >
            {(staff.data ?? []).map((member) => (
              <option key={member.user_id} value={member.user_id}>
                {member.name}
              </option>
            ))}
            <option value="none">{t("client.todo.assign.nobody")}</option>
          </select>
        </div>

        {result !== null && result.assigned > 0 ? (
          <p role="status" data-testid="firm-assign-done">
            {t("client.todo.assign.done", { count: result.assigned })}
          </p>
        ) : null}
        {result !== null && result.failed.length > 0 ? (
          <FailureList
            heading={t("client.todo.assign.failed", { count: result.failed.length })}
            items={result.failed.map((failure) => ({
              key: failure.administration_id,
              label: nameOf(failure.administration_id),
              reason: failure.reason,
            }))}
            testId="firm-assign-failures"
          />
        ) : null}
        {problem !== null ? (
          <p className="field-error" role="alert">
            {problem}
          </p>
        ) : null}

        <div className="dialog__actions">
          <Button onClick={onClose}>{t("common.action.cancel")}</Button>
          <Button
            type="submit"
            variant="primary"
            disabled={busy || userId === ""}
            data-testid="firm-assign-submit"
          >
            {busy ? t("client.todo.working") : t("client.todo.bulk.assign")}
          </Button>
        </div>
      </form>
    </FirmDialog>
  );
}

/** Snooze one client until a date, with a reason colleagues can read; or end a snooze. */
export function SnoozeDialog({
  api,
  row,
  onClose,
  onDone,
}: {
  api: FirmApiShape;
  row: FirmWorklistRowView;
  onClose: () => void;
  onDone: () => unknown;
}) {
  const { t } = useI18n();
  const today = new Date();
  const [until, setUntil] = useState(() => row.snoozed_until ?? isoDay(addDays(today, 7)));
  const [reason, setReason] = useState(row.snooze_reason ?? "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const untilId = useId();
  const reasonId = useId();
  const hintId = useId();

  const send = async (value: string | null) => {
    setBusy(true);
    setProblem(null);
    try {
      await api.snooze(row.administration_id, value, value === null ? "" : reason.trim());
      void onDone();
      onClose();
    } catch (error) {
      setProblem(describeError(error));
      setBusy(false);
    }
  };

  return (
    <FirmDialog
      title={t("client.todo.snooze.title", { name: row.display_name })}
      onClose={onClose}
      testId="firm-snooze-dialog"
    >
      <form
        className="firm-form"
        onSubmit={(event) => {
          event.preventDefault();
          void send(until);
        }}
      >
        <div className="ui-field">
          <label className="ui-label" htmlFor={untilId}>
            {t("client.todo.snooze.until")}
          </label>
          <input
            id={untilId}
            type="date"
            className="ui-input"
            required
            min={isoDay(addDays(today, 1))}
            value={until}
            data-testid="firm-snooze-until"
            onChange={(event) => setUntil(event.target.value)}
          />
        </div>
        <div className="ui-field">
          <label className="ui-label" htmlFor={reasonId}>
            {t("client.todo.snooze.reason")}
          </label>
          <textarea
            id={reasonId}
            className="ui-input firm-textarea"
            aria-describedby={hintId}
            value={reason}
            data-testid="firm-snooze-reason"
            onChange={(event) => setReason(event.target.value)}
          />
          <p id={hintId} className="ui-help">
            {t("client.todo.snooze.reason_hint")}
          </p>
        </div>
        {problem !== null ? (
          <p className="field-error" role="alert">
            {problem}
          </p>
        ) : null}
        <div className="dialog__actions">
          {row.snoozed_until !== null ? (
            <Button disabled={busy} onClick={() => void send(null)} data-testid="firm-snooze-clear">
              {t("client.todo.snooze.clear")}
            </Button>
          ) : null}
          <Button onClick={onClose}>{t("common.action.cancel")}</Button>
          <Button
            type="submit"
            variant="primary"
            disabled={busy || until === ""}
            data-testid="firm-snooze-submit"
          >
            {busy ? t("client.todo.working") : t("client.todo.snooze.submit")}
          </Button>
        </div>
      </form>
    </FirmDialog>
  );
}
