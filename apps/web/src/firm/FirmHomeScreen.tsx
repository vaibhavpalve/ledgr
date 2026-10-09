import { useCallback, useMemo, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { PanelRightClose, PanelRightOpen, TriangleAlert } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type {
  FirmSummaryCounts,
  FirmSummaryView,
  FirmWorklistChip,
  FirmWorklistRowView,
  FirmWorklistSort,
} from "@ledgr/shared-types";

import "./FirmHome.css";
import { describeError } from "../api/http";
import { useSession } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { Button } from "../ui";
import type { FirmApiShape } from "./api";
import { AssignDialog, SnoozeDialog } from "./FirmDialogs";
import { ReviewSheet } from "./ReviewSheet";
import { AwayPanel, DeadlinesPanel, RepliesPanel } from "./SidePanels";
import {
  daysSince,
  localDateOf,
  useDebounced,
  useMediaQuery,
  useResource,
  type Resource,
} from "./useResource";
import { Worklist } from "./Worklist";

/**
 * `/todo` - the firm home (docs/firm-home/contract.md; FR-FRM-001, FR-FRM-002, FR-UX-005): where
 * an accountant lands when no client is open. Six portfolio counts, the cross-client worklist,
 * and a right column (since you were away, deadlines, client replies).
 *
 * Every figure here comes from the server per request and is restricted there to the clients the
 * caller holds a live grant on (ADR-109). Nothing on this page is an authorization decision:
 * hiding a button is presentation, the API refuses on its own (CLAUDE.md rule 3).
 *
 * A business user has no firm home; `/todo` sends them to their own dashboard.
 */
export function FirmHomeRoute() {
  const { me } = useSession();
  const { firm } = useServices();
  if (me.organization.kind !== "firm") return <Navigate to="/" replace />;
  return <FirmHomeScreen api={firm} />;
}

type CountKey = keyof FirmSummaryCounts;

/** Where each count in the strip takes the worklist. */
const COUNT_FILTER: Record<CountKey, { chip: FirmWorklistChip; sort: FirmWorklistSort }> = {
  auto_bookings: { chip: "my_move", sort: "risk" },
  receipts_to_book: { chip: "my_move", sort: "to_book" },
  missing_receipts: { chip: "waiting_on_client", sort: "missing_receipts" },
  bank_to_match: { chip: "my_move", sort: "bank_to_match" },
  open_questions: { chip: "waiting_on_client", sort: "open_questions" },
  broken_feeds: { chip: "waiting_on_client", sort: "risk" },
};

const COUNT_ORDER: readonly CountKey[] = [
  "auto_bookings",
  "receipts_to_book",
  "missing_receipts",
  "bank_to_match",
  "open_questions",
  "broken_feeds",
];

const PAGE_SIZE = 50;
/** "Show all": one page as large as the list. The contract names no maximum page size. */
const SHOW_ALL_SIZE = 1000;

/** Count sorts start with the largest; name, dates and risk with the smallest. */
const DESC_FIRST: ReadonlySet<FirmWorklistSort> = new Set([
  "to_book",
  "missing_receipts",
  "bank_to_match",
  "open_questions",
]);

export function FirmHomeScreen({ api }: { api: FirmApiShape }) {
  const { t, date } = useI18n();
  const navigate = useNavigate();
  const { administrations, administration, switchAdministration } = useSession();

  // --- the worklist's query, all of it server-side ---
  const [chip, setChip] = useState<FirmWorklistChip>("my_move");
  const [assigned, setAssigned] = useState<"me" | "any">("me");
  const [query, setQuery] = useState("");
  const q = useDebounced(query, 300);
  const [sort, setSort] = useState<FirmWorklistSort>("risk");
  const [dir, setDir] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZE);

  const loadSummary = useCallback(() => api.getSummary(), [api]);
  const loadWorklist = useCallback(
    () => api.getWorklist({ chip, q, assigned, sort, dir, page, pageSize }),
    [api, chip, q, assigned, sort, dir, page, pageSize],
  );
  const loadDeadlines = useCallback(() => api.getDeadlines(), [api]);
  const loadInbox = useCallback(() => api.getInbox({ limit: 3 }), [api]);

  const summary = useResource(loadSummary);
  const worklist = useResource(loadWorklist);
  const deadlines = useResource(loadDeadlines);
  const inbox = useResource(loadInbox);

  // --- selection, dialogs, opening a client ---
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [review, setReview] = useState<{ ids?: readonly string[] } | null>(null);
  const [assigning, setAssigning] = useState<
    readonly Pick<FirmWorklistRowView, "administration_id" | "display_name">[] | null
  >(null);
  const [snoozing, setSnoozing] = useState<FirmWorklistRowView | null>(null);
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [markingSeen, setMarkingSeen] = useState(false);
  const [sideOpen, setSideOpen] = useState(true);
  const narrow = useMediaQuery("(max-width: 39.99rem)");

  const pick = useCallback((nextChip: FirmWorklistChip, nextSort: FirmWorklistSort) => {
    setChip(nextChip);
    setSort(nextSort);
    setDir(DESC_FIRST.has(nextSort) ? "desc" : "asc");
    setPage(1);
  }, []);

  const onSort = useCallback(
    (column: FirmWorklistSort) => {
      if (column === sort) {
        setDir((value) => (value === "asc" ? "desc" : "asc"));
      } else {
        setSort(column);
        setDir(DESC_FIRST.has(column) ? "desc" : "asc");
      }
      setPage(1);
    },
    [sort],
  );

  const onToggle = useCallback((id: string) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const onToggleAll = useCallback((ids: readonly string[], on: boolean) => {
    setSelected((previous) => {
      const next = new Set(previous);
      for (const id of ids) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }, []);

  const { reload: reloadSummary } = summary;
  const { reload: reloadWorklist } = worklist;
  const { reload: reloadDeadlines } = deadlines;
  const refreshAll = useCallback(() => {
    reloadSummary();
    reloadWorklist();
    reloadDeadlines();
  }, [reloadSummary, reloadWorklist, reloadDeadlines]);

  /** Same switch the portfolio uses (ClientsScreen.open): the session's client, then its home. */
  const open = async (row: FirmWorklistRowView) => {
    setOpeningId(row.administration_id);
    setProblem(null);
    try {
      if (row.administration_id !== administration?.id) {
        await switchAdministration(row.administration_id);
      }
      navigate("/");
    } catch (error) {
      setProblem(describeError(error));
      setOpeningId(null);
    }
  };

  const markSeen = async () => {
    setMarkingSeen(true);
    try {
      await api.markSeen();
      summary.reload();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setMarkingSeen(false);
    }
  };

  const selectedRows = useMemo(() => {
    const visible = worklist.data?.rows ?? [];
    return [...selected].map(
      (id) =>
        visible.find((row) => row.administration_id === id) ?? {
          administration_id: id,
          display_name: administrations.find((entry) => entry.id === id)?.legal_name ?? id,
        },
    );
  }, [selected, worklist.data, administrations]);

  const dialogOpen = review !== null || assigning !== null || snoozing !== null;

  // FR-UX-004: a firm with no clients is taught what this page is for, and how to start.
  if (administrations.length === 0) {
    return (
      <section
        className="screen firm-home"
        aria-label={t("client.todo.title")}
        data-testid="firm-home"
      >
        <PageHeader title={t("client.todo.title")} />
        <EmptyState
          icon={<Icon name="clients" size={32} />}
          title={t("client.todo.empty_title")}
          body={t("client.todo.empty_body")}
          action={
            <Link
              to="/onboarding"
              className="button-link button-link--primary"
              data-testid="firm-add-first"
            >
              {t("client.portfolio.add_first")}
            </Link>
          }
          testId="firm-home-empty"
        />
      </section>
    );
  }

  const autoBookings = summary.data?.counts.auto_bookings ?? 0;

  return (
    <section
      className="screen firm-home"
      aria-label={t("client.todo.title")}
      data-testid="firm-home"
    >
      <PageHeader
        title={t("client.todo.title")}
        context={summary.data !== null ? welcomeLine(summary.data, t, date) : undefined}
        action={
          autoBookings > 0 ? (
            <Button variant="primary" onClick={() => setReview({})} data-testid="firm-review-open">
              {t("client.todo.review_button", { count: autoBookings })}
            </Button>
          ) : undefined
        }
      />

      {problem !== null ? <ErrorState message={problem} testId="firm-problem" /> : null}

      <CountStrip summary={summary} onPick={pick} />

      <div className={`firm-home__layout${sideOpen ? "" : " firm-home__layout--wide"}`}>
        <div className="firm-home__main">
          {selected.size > 0 ? (
            <div
              className="firm-bulk"
              role="region"
              aria-label={t("client.todo.bulk.label")}
              data-testid="firm-bulk"
            >
              <span className="firm-bulk__count">
                {t("client.todo.bulk.selected", { count: selected.size })}
              </span>
              <Button
                size="sm"
                onClick={() => setReview({ ids: [...selected] })}
                data-testid="firm-bulk-review"
              >
                {t("client.todo.bulk.review")}
              </Button>
              <Button
                size="sm"
                onClick={() => setAssigning(selectedRows)}
                data-testid="firm-bulk-assign"
              >
                {t("client.todo.bulk.assign")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSelected(new Set())}
                data-testid="firm-bulk-clear"
              >
                {t("client.todo.bulk.clear")}
              </Button>
            </div>
          ) : null}

          <Worklist
            resource={worklist}
            chip={chip}
            onChip={(value) => {
              setChip(value);
              setPage(1);
            }}
            assigned={assigned}
            onAssigned={(value) => {
              setAssigned(value);
              setPage(1);
            }}
            query={query}
            onQuery={(value) => {
              setQuery(value);
              setPage(1);
            }}
            sort={sort}
            dir={dir}
            onSort={onSort}
            page={page}
            pageSize={pageSize}
            onPage={setPage}
            onShowAll={() => {
              setPage(1);
              setPageSize(SHOW_ALL_SIZE);
            }}
            selected={selected}
            onToggle={onToggle}
            onToggleAll={onToggleAll}
            onOpen={open}
            openingId={openingId}
            onSnooze={setSnoozing}
            onAssignRows={setAssigning}
            narrow={narrow}
            keyboardEnabled={!dialogOpen}
          />
        </div>

        <aside className="firm-home__side">
          <Button
            size="sm"
            variant="ghost"
            className="firm-side-toggle"
            aria-expanded={sideOpen}
            onClick={() => setSideOpen((value) => !value)}
            data-testid="firm-side-toggle"
          >
            {sideOpen ? (
              <PanelRightClose size={16} aria-hidden="true" />
            ) : (
              <PanelRightOpen size={16} aria-hidden="true" />
            )}
            {sideOpen ? t("client.todo.side.hide") : t("client.todo.side.show")}
          </Button>
          {sideOpen ? (
            <div className="firm-home__panels">
              <AwayPanel
                summary={summary}
                onPick={pick}
                onMarkSeen={markSeen}
                markingSeen={markingSeen}
              />
              <DeadlinesPanel deadlines={deadlines} />
              <RepliesPanel inbox={inbox} />
            </div>
          ) : null}
        </aside>
      </div>

      {review !== null ? (
        <ReviewSheet
          api={api}
          administrationIds={review.ids}
          onClose={() => setReview(null)}
          onChanged={refreshAll}
        />
      ) : null}
      {assigning !== null ? (
        <AssignDialog
          api={api}
          rows={assigning}
          onClose={() => setAssigning(null)}
          onDone={() => {
            setSelected(new Set());
            worklist.reload();
          }}
        />
      ) : null}
      {snoozing !== null ? (
        <SnoozeDialog
          api={api}
          row={snoozing}
          onClose={() => setSnoozing(null)}
          onDone={refreshAll}
        />
      ) : null}
    </section>
  );
}

function welcomeLine(
  summary: FirmSummaryView,
  t: (key: string, params?: Readonly<Record<string, string | number>>) => string,
  date: (iso: string, style?: "short" | "long" | "iso") => string,
): string | undefined {
  if (summary.previous_login_at === null) return undefined;
  const day = localDateOf(summary.previous_login_at);
  if (day === null) return undefined;
  const days = Math.max(0, daysSince(day));
  return t("client.todo.welcome", {
    date: date(day, "long"),
    ago: days === 0 ? t("client.todo.today") : t("client.todo.days_ago", { count: days }),
  });
}

/**
 * FR-FRM-001's portfolio counts. Each is a button that points the worklist at the clients behind
 * it. A broken bank feed is the one count in the negative colour - always with an icon and a
 * word, never colour alone.
 */
function CountStrip({
  summary,
  onPick,
}: {
  summary: Resource<FirmSummaryView>;
  onPick: (chip: FirmWorklistChip, sort: FirmWorklistSort) => void;
}) {
  const { t } = useI18n();
  if (summary.data === null && summary.loading) {
    return <LoadingSkeleton rows={1} testId="firm-counts-loading" />;
  }
  if (summary.data === null) {
    return (
      <ErrorState
        message={summary.error ?? t("client.todo.panel_error")}
        onRetry={summary.reload}
        testId="firm-counts-error"
      />
    );
  }
  const counts = summary.data.counts;
  return (
    <ul
      className="firm-counts"
      aria-label={t("client.todo.counts_label")}
      data-testid="firm-counts"
    >
      {COUNT_ORDER.map((key) => {
        const value = counts[key];
        const broken = key === "broken_feeds" && value > 0;
        const filter = COUNT_FILTER[key];
        return (
          <li key={key}>
            <button
              type="button"
              className={`firm-count-tile${broken ? " firm-count-tile--negative" : ""}`}
              data-testid={`firm-count-${key}`}
              onClick={() => onPick(filter.chip, filter.sort)}
            >
              <span className="firm-count-tile__value ui-num">{value}</span>
              <span className="firm-count-tile__label">{t(`client.todo.count.${key}`)}</span>
              {broken ? (
                <span className="firm-count-tile__flag">
                  <TriangleAlert size={14} aria-hidden="true" />
                  {t("client.todo.count.needs_attention")}
                </span>
              ) : null}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
