import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type {
  FirmActivityView,
  FirmDeadlineView,
  FirmInboxView,
  FirmSummaryView,
  FirmWorklistChip,
  FirmWorklistSort,
} from "@ledgr/shared-types";

import { LoadingSkeleton } from "../shell/ScreenState";
import { Button } from "../ui";
import type { Resource } from "./useResource";
import { localDateOf } from "./useResource";

/**
 * The right column of the firm home. Each panel owns its own request and fails on its own: an
 * endpoint that is not deployed yet (the inbox, while FR-FRM-005 lands) costs that panel one
 * quiet line and a retry, and the rest of the page keeps working.
 */
export function Panel({
  title,
  aside,
  children,
  testId,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  testId: string;
}) {
  return (
    <section className="firm-panel" aria-label={title} data-testid={testId}>
      <div className="firm-panel__head">
        <h2 className="firm-panel__title">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** One quiet line where a panel would be, with a way to try again (D5). */
export function PanelError({ onRetry, testId }: { onRetry: () => void; testId: string }) {
  const { t } = useI18n();
  return (
    <p className="firm-panel__error" data-testid={testId}>
      <span>{t("client.todo.panel_error")}</span>{" "}
      <button type="button" className="ui-textbutton" onClick={onRetry}>
        {t("common.action.retry")}
      </button>
    </p>
  );
}

/** Where each "since you were away" line takes the worklist. */
export const ACTIVITY_FILTER: Record<
  FirmActivityView["kind"],
  { chip: FirmWorklistChip; sort: FirmWorklistSort }
> = {
  receipts_uploaded: { chip: "my_move", sort: "to_book" },
  auto_bookings_ready: { chip: "my_move", sort: "risk" },
  client_replies: { chip: "my_move", sort: "open_questions" },
  bank_feeds_broken: { chip: "waiting_on_client", sort: "risk" },
  possible_duplicates: { chip: "my_move", sort: "risk" },
  // Wave 2 (ADR-113): bookings a rule posted on its own; each client's Rules screen lists them.
  rule_postings: { chip: "my_move", sort: "risk" },
};

export function AwayPanel({
  summary,
  onPick,
  onMarkSeen,
  markingSeen,
}: {
  summary: Resource<FirmSummaryView>;
  onPick: (chip: FirmWorklistChip, sort: FirmWorklistSort) => void;
  onMarkSeen: () => unknown;
  markingSeen: boolean;
}) {
  const { t, date } = useI18n();
  const data = summary.data;
  const sinceDay = data !== null ? localDateOf(data.since) : null;
  // A kind this build does not know yet is skipped rather than thrown on (`t` refuses unknown keys).
  const activity = (data?.activity ?? []).filter(
    (line) => line.count > 0 && line.kind in ACTIVITY_FILTER,
  );

  return (
    <Panel
      title={t("client.todo.away.title")}
      testId="firm-away"
      aside={
        sinceDay !== null ? (
          <span className="firm-muted firm-panel__range">
            {t("client.todo.away.range", { date: date(sinceDay, "long") })}
          </span>
        ) : undefined
      }
    >
      {data === null && summary.loading ? (
        <LoadingSkeleton rows={2} testId="firm-away-loading" />
      ) : data === null ? (
        <PanelError onRetry={summary.reload} testId="firm-away-error" />
      ) : activity.length === 0 ? (
        <p className="firm-muted">{t("client.todo.away.nothing")}</p>
      ) : (
        <>
          <ul className="firm-panel__list">
            {activity.map((line) => {
              const shown = line.clients.map((client) => client.display_name).join(", ");
              const more = line.client_count - line.clients.length;
              const filter = ACTIVITY_FILTER[line.kind];
              return (
                <li key={line.kind}>
                  <button
                    type="button"
                    className="firm-activity"
                    data-testid={`firm-activity-${line.kind}`}
                    onClick={() => onPick(filter.chip, filter.sort)}
                  >
                    <span className="firm-activity__what">
                      {t(`client.todo.activity.${line.kind}`, { count: line.count })}
                    </span>
                    <span className="firm-muted firm-activity__who">
                      {shown}
                      {more > 0 ? ` ${t("client.todo.activity.more", { count: more })}` : null}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <Button
            size="sm"
            variant="ghost"
            disabled={markingSeen}
            onClick={() => void onMarkSeen()}
            data-testid="firm-mark-seen"
          >
            {t("client.todo.away.mark_seen")}
          </Button>
        </>
      )}
    </Panel>
  );
}

const BUCKETS = [
  "filed",
  "ready_to_file",
  "ready_to_review",
  "in_progress",
  "not_started",
] as const;

export function DeadlinesPanel({ deadlines }: { deadlines: Resource<FirmDeadlineView[]> }) {
  const { t, date } = useI18n();
  const data = deadlines.data;

  return (
    <Panel title={t("client.todo.deadlines.title")} testId="firm-deadlines">
      {data === null && deadlines.loading ? (
        <LoadingSkeleton rows={2} testId="firm-deadlines-loading" />
      ) : data === null ? (
        <PanelError onRetry={deadlines.reload} testId="firm-deadlines-error" />
      ) : data.length === 0 ? (
        <p className="firm-muted">{t("client.todo.deadlines.none")}</p>
      ) : (
        <ul className="firm-panel__list">
          {data.map((deadline) => {
            const total =
              BUCKETS.reduce((sum, bucket) => sum + deadline.buckets[bucket], 0) ||
              deadline.client_count;
            const label = t("client.todo.deadlines.bar_label", {
              filed: deadline.buckets.filed,
              total,
              ready: deadline.buckets.ready_to_file,
              review: deadline.buckets.ready_to_review,
              progress: deadline.buckets.in_progress,
              open: deadline.buckets.not_started,
            });
            return (
              <li
                key={`${deadline.kind}-${deadline.period_label}`}
                className="firm-deadline"
                data-testid={`firm-deadline-${deadline.period_label}`}
              >
                <div className="firm-deadline__head">
                  <span className="firm-deadline__name">
                    {t("client.todo.deadlines.vat", { period: deadline.period_label })}
                  </span>
                  <span className="firm-muted">
                    {t("client.todo.deadlines.due", { date: date(deadline.due_date) })}
                  </span>
                </div>
                <div className="firm-bar" role="img" aria-label={label}>
                  {BUCKETS.map((bucket) =>
                    deadline.buckets[bucket] > 0 ? (
                      <span
                        key={bucket}
                        className={`firm-bar__part firm-bar__part--${bucket}`}
                        style={{ flexGrow: deadline.buckets[bucket] }}
                      />
                    ) : null,
                  )}
                </div>
                <p className="firm-deadline__legend" aria-hidden="true">
                  {BUCKETS.filter((bucket) => deadline.buckets[bucket] > 0).map((bucket) => (
                    <span key={bucket} className="firm-deadline__key">
                      <span className={`firm-dot firm-bar__part--${bucket}`} />
                      {t(`client.todo.deadlines.bucket.${bucket}`, {
                        count: deadline.buckets[bucket],
                      })}
                    </span>
                  ))}
                </p>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

/**
 * "Client replies": the caller loads it with `awaiting=firm` (ADR-111), so it lists only threads
 * where the client wrote last - never the firm's own questions still waiting on the client.
 */
export function RepliesPanel({ inbox }: { inbox: Resource<FirmInboxView> }) {
  const { t, date } = useI18n();
  const data = inbox.data;

  return (
    <Panel
      title={t("client.todo.replies.title")}
      testId="firm-replies"
      aside={
        <Link to="/inbox" className="ui-textbutton" data-testid="firm-replies-inbox">
          {t("client.todo.replies.open_inbox")}
        </Link>
      }
    >
      {data === null && inbox.loading ? (
        <LoadingSkeleton rows={2} testId="firm-replies-loading" />
      ) : data === null ? (
        <PanelError onRetry={inbox.reload} testId="firm-replies-error" />
      ) : data.items.length === 0 ? (
        <p className="firm-muted">{t("client.todo.replies.none")}</p>
      ) : (
        <ul className="firm-panel__list">
          {data.items.map((item) => {
            const day = localDateOf(item.last_message_at);
            return (
              <li
                key={item.thread_id}
                className="firm-reply"
                data-testid={`firm-reply-${item.thread_id}`}
              >
                <span className="firm-reply__head">
                  <span className="firm-reply__client">{item.display_name}</span>
                  {item.unread ? (
                    <span className="chip chip--accent">{t("client.todo.replies.unread")}</span>
                  ) : null}
                  {day !== null ? <span className="firm-muted">{date(day)}</span> : null}
                </span>
                <span className="firm-reply__subject">{item.subject}</span>
                <span className="firm-muted firm-reply__excerpt">{item.excerpt}</span>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
