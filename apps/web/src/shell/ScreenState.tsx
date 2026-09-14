import type { ReactNode } from "react";
import { useI18n } from "@ledgr/i18n";

/**
 * The four states every screen in the routed app has, built once so that
 * "a teaching empty state, loading skeletons, an error state that says what
 * to do" (the brief's own list; FR-UX-004, D5) is a matter of using these
 * rather than remembering them.
 *
 *   PageHeader        the h1, its context line, and the ONE primary action
 *                     (D1) — at most one filled button per screen lives here.
 *   LoadingSkeleton   holds the space the content will take, announced once
 *                     via role="status" (the same shape HomeScreen's own
 *                     skeleton already had).
 *   ErrorState        what happened (the server's own sentence, FR-UX-007)
 *                     and what to do next: retry, and optionally a way out.
 *   EmptyState        what belongs here and the action that creates the first
 *                     item — never a bare "no results".
 */

export function PageHeader({
  title,
  context,
  action,
  children,
}: {
  title: string;
  /** A date, a count, a fiscal year — the one line beside the title in Main.dc.html. */
  context?: string;
  /** The screen's single primary action (D1). */
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div className="page-header__titles">
        <h1>{title}</h1>
        {context !== undefined ? <p className="page-header__context">{context}</p> : null}
      </div>
      {children}
      {action !== undefined ? <div className="page-header__action">{action}</div> : null}
    </div>
  );
}

export function LoadingSkeleton({ rows = 3, testId }: { rows?: number; testId?: string }) {
  const { t } = useI18n();
  return (
    <div role="status" className="skeleton-list" data-testid={testId ?? "screen-loading"}>
      <span className="ledgr-visually-hidden">{t("mobile.common.loading")}</span>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton skeleton-list__row" aria-hidden="true" />
      ))}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
  children,
  testId,
}: {
  /** The server's own sentence, or the network message. */
  message: string;
  onRetry?: () => void;
  /** A further way out — a link back, a sign-out — for when retrying is not the answer. */
  children?: ReactNode;
  testId?: string;
}) {
  const { t } = useI18n();
  return (
    <div role="alert" className="alert alert--attention error-state" data-testid={testId ?? "screen-error"}>
      <div className="error-state__text">
        <p className="error-state__title">{t("common.error.title")}</p>
        <p>{message}</p>
        <p className="error-state__hint">{t("common.error.next_step")}</p>
      </div>
      <div className="error-state__actions">
        {onRetry !== undefined ? (
          <button type="button" onClick={onRetry} data-testid="screen-error-retry">
            {t("common.action.retry")}
          </button>
        ) : null}
        {children}
      </div>
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  body,
  action,
  testId,
}: {
  icon?: ReactNode;
  title: string;
  body: string;
  /** The action that creates the first item (FR-UX-004). */
  action?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="panel empty-state" data-testid={testId ?? "screen-empty"}>
      {icon !== undefined ? (
        <span className="empty-state__icon" aria-hidden="true">
          {icon}
        </span>
      ) : null}
      <p className="empty-state__title">{title}</p>
      <p className="empty-state__body">{body}</p>
      {action !== undefined ? <div className="empty-state__action">{action}</div> : null}
    </div>
  );
}
