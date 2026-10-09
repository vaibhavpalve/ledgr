import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
} from "react";
import { ArrowDown, ArrowUp, Ellipsis, TriangleAlert } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type {
  FirmVatStatus,
  FirmWorklistChip,
  FirmWorklistRowView,
  FirmWorklistSort,
  FirmWorklistView,
} from "@ledgr/shared-types";

import { ErrorState, LoadingSkeleton } from "../shell/ScreenState";
import { Badge, Button, type BadgeVariant } from "../ui";
import type { Resource } from "./useResource";

/**
 * The cross-client work queue (FR-FRM-002): one row per client, filtered server-side by chip,
 * assignee and free text, sorted server-side. On a phone the same rows are a card list for
 * triage, so the page never scrolls sideways.
 *
 * Keyboard (D8, FR-WEB-002): J / K move between rows, Enter opens the focused client, X selects
 * it. Keys are ignored while typing in a field or while a dialog is open.
 */

export const CHIPS: readonly Exclude<FirmWorklistChip, "all">[] = [
  "my_move",
  "waiting_on_client",
  "vat_not_filed",
  "books_behind",
  "up_to_date",
  "snoozed",
];

const VAT_BADGE: Record<FirmVatStatus, BadgeVariant> = {
  not_started: "neutral",
  in_progress: "neutral",
  ready_to_review: "draft",
  ready_to_file: "review",
  filed: "booked",
};

export interface WorklistProps {
  resource: Resource<FirmWorklistView>;
  chip: FirmWorklistChip;
  onChip: (chip: FirmWorklistChip) => void;
  assigned: "me" | "any";
  onAssigned: (value: "me" | "any") => void;
  query: string;
  onQuery: (value: string) => void;
  sort: FirmWorklistSort;
  dir: "asc" | "desc";
  onSort: (sort: FirmWorklistSort) => void;
  page: number;
  pageSize: number;
  onPage: (page: number) => void;
  onShowAll: () => void;
  selected: ReadonlySet<string>;
  onToggle: (id: string) => void;
  onToggleAll: (ids: readonly string[], on: boolean) => void;
  onOpen: (row: FirmWorklistRowView) => unknown;
  openingId: string | null;
  onSnooze: (row: FirmWorklistRowView) => void;
  onAssignRows: (rows: readonly FirmWorklistRowView[]) => void;
  narrow: boolean;
  keyboardEnabled: boolean;
}

export function Worklist(props: WorklistProps) {
  const { t } = useI18n();
  const { resource, chip, query } = props;
  const data = resource.data;
  const rows = useMemo(() => data?.rows ?? [], [data]);
  const [active, setActive] = useState(-1);
  const openRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const filterId = useId();

  // A new answer is a new list: the cursor starts over.
  useEffect(() => setActive(-1), [data]);

  const { keyboardEnabled, onToggle, onOpen, openingId } = props;
  useEffect(() => {
    if (!keyboardEnabled || rows.length === 0) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (
        target !== null &&
        (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
      ) {
        return;
      }
      const key = event.key.toLowerCase();
      if (key === "j" || key === "k") {
        event.preventDefault();
        const next =
          key === "j"
            ? Math.min(active + 1, rows.length - 1)
            : Math.max(active === -1 ? 0 : active - 1, 0);
        setActive(next);
        openRefs.current[next]?.focus();
      } else if (key === "x" && active >= 0) {
        const row = rows[active];
        if (row !== undefined) {
          event.preventDefault();
          onToggle(row.administration_id);
        }
      } else if (key === "enter" && active >= 0 && target === openRefs.current[active]) {
        // The client's name is the row's open control; Enter on it opens the client. Handled
        // here (and the button's own activation suppressed) so it is one open, not two.
        const row = rows[active];
        if (row !== undefined && openingId === null) {
          event.preventDefault();
          void onOpen(row);
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [keyboardEnabled, rows, active, onToggle, onOpen, openingId]);

  const counts = data?.chip_counts;

  return (
    <section
      className="firm-worklist"
      aria-labelledby={`${filterId}-heading`}
      data-testid="firm-worklist"
    >
      <h2 id={`${filterId}-heading`} className="ledgr-visually-hidden">
        {t("client.todo.table_caption")}
      </h2>
      <div className="firm-worklist__controls">
        <div className="firm-chips" role="group" aria-label={t("client.todo.chips_label")}>
          {CHIPS.map((value) => (
            <button
              key={value}
              type="button"
              className="firm-chip"
              aria-pressed={chip === value}
              data-testid={`firm-chip-${value}`}
              onClick={() => props.onChip(value)}
            >
              <span>{t(`client.todo.chip.${value}`)}</span>
              {counts !== undefined ? (
                <span className="firm-chip__count ui-num">{counts[value]}</span>
              ) : null}
            </button>
          ))}
        </div>
        <div className="firm-worklist__filters">
          <div
            className="bk-seg firm-seg"
            role="group"
            aria-label={t("client.todo.assigned.label")}
          >
            {(["me", "any"] as const).map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={props.assigned === value}
                data-testid={`firm-assigned-${value}`}
                onClick={() => props.onAssigned(value)}
              >
                {t(`client.todo.assigned.${value}`)}
              </button>
            ))}
          </div>
          <label className="ledgr-visually-hidden" htmlFor={filterId}>
            {t("client.todo.filter.label")}
          </label>
          <input
            id={filterId}
            type="search"
            className="ui-input firm-filter"
            value={query}
            placeholder={t("client.todo.filter.placeholder")}
            data-testid="firm-filter"
            onChange={(event) => props.onQuery(event.target.value)}
          />
        </div>
      </div>

      <div aria-busy={resource.loading} className="firm-worklist__body">
        {data === null && resource.loading ? (
          <LoadingSkeleton rows={6} testId="firm-worklist-loading" />
        ) : data === null && resource.error !== null ? (
          <ErrorState
            message={resource.error}
            onRetry={resource.reload}
            testId="firm-worklist-error"
          />
        ) : rows.length === 0 ? (
          <EmptyList chip={chip} query={query} onClear={() => props.onQuery("")} />
        ) : props.narrow ? (
          <CardList
            {...props}
            rows={rows}
            active={active}
            setActive={setActive}
            openRefs={openRefs}
          />
        ) : (
          <Table {...props} rows={rows} active={active} setActive={setActive} openRefs={openRefs} />
        )}
        {data !== null && resource.error !== null ? (
          <ErrorState
            message={resource.error}
            onRetry={resource.reload}
            testId="firm-worklist-error"
          />
        ) : null}
      </div>

      {data !== null && rows.length > 0 ? (
        <Pager
          page={data.page}
          pageSize={data.page_size}
          shown={rows.length}
          total={data.total}
          onPage={props.onPage}
          onShowAll={props.onShowAll}
        />
      ) : null}
      {!props.narrow && rows.length > 0 ? (
        <p className="firm-muted firm-worklist__hint" aria-hidden="true">
          {t("client.todo.keyboard_hint")}
        </p>
      ) : null}
    </section>
  );
}

function EmptyList({
  chip,
  query,
  onClear,
}: {
  chip: FirmWorklistChip;
  query: string;
  onClear: () => void;
}) {
  const { t } = useI18n();
  if (query.trim() !== "") {
    return (
      <div className="ui-empty" data-testid="firm-worklist-empty">
        <p className="ui-empty__title">
          {t("client.todo.list_no_matches", { query: query.trim() })}
        </p>
        <Button size="sm" onClick={onClear}>
          {t("client.todo.clear_filter")}
        </Button>
      </div>
    );
  }
  return (
    <div className="ui-empty" data-testid="firm-worklist-empty">
      <p className="ui-empty__title">
        {chip === "my_move" ? t("client.todo.list_empty_my_move") : t("client.todo.list_empty")}
      </p>
    </div>
  );
}

interface RowsProps extends WorklistProps {
  rows: readonly FirmWorklistRowView[];
  active: number;
  setActive: (index: number) => void;
  openRefs: MutableRefObject<(HTMLButtonElement | null)[]>;
}

const SORTABLE: readonly { sort: FirmWorklistSort; key: string; numeric: boolean }[] = [
  { sort: "name", key: "client.todo.col.client", numeric: false },
  { sort: "booked_until", key: "client.todo.col.booked_until", numeric: false },
  { sort: "to_book", key: "client.todo.col.to_book", numeric: true },
  { sort: "missing_receipts", key: "client.todo.col.missing_receipts", numeric: true },
  { sort: "bank_to_match", key: "client.todo.col.bank", numeric: true },
  { sort: "open_questions", key: "client.todo.col.questions", numeric: true },
];

function Table(props: RowsProps) {
  const { t } = useI18n();
  const { rows, selected, sort, dir } = props;
  const ids = rows.map((row) => row.administration_id);
  const allSelected = ids.length > 0 && ids.every((id) => selected.has(id));

  const header = (column: FirmWorklistSort, label: string, numeric: boolean) => (
    <th
      key={column}
      scope="col"
      className={numeric ? "ui-num" : undefined}
      aria-sort={sort === column ? (dir === "asc" ? "ascending" : "descending") : undefined}
    >
      <button
        type="button"
        className="firm-sort"
        aria-label={t("client.todo.sort_by", { column: label })}
        data-testid={`firm-sort-${column}`}
        onClick={() => props.onSort(column)}
      >
        <span aria-hidden="true">{label}</span>
        {sort === column ? (
          dir === "asc" ? (
            <ArrowUp size={12} aria-hidden="true" />
          ) : (
            <ArrowDown size={12} aria-hidden="true" />
          )
        ) : null}
      </button>
    </th>
  );

  return (
    <div className="firm-tablewrap">
      <table className="ui-table firm-table" data-testid="firm-worklist-table">
        <caption className="ledgr-visually-hidden">{t("client.todo.table_caption")}</caption>
        <thead>
          <tr>
            <th scope="col" className="firm-table__select">
              <input
                type="checkbox"
                aria-label={t("client.todo.col.select")}
                checked={allSelected}
                data-testid="firm-select-all"
                onChange={() => props.onToggleAll(ids, !allSelected)}
              />
            </th>
            {SORTABLE.map((column) => header(column.sort, t(column.key), column.numeric))}
            <th scope="col">{t("client.todo.col.vat")}</th>
            {header("vat_due", t("client.todo.col.due"), true)}
            <th scope="col">
              <span className="ledgr-visually-hidden">{t("client.todo.col.actions")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={row.administration_id}
              className={index === props.active ? "firm-row firm-row--active" : "firm-row"}
              aria-selected={selected.has(row.administration_id) ? true : undefined}
              data-testid={`firm-row-${row.administration_id}`}
              onFocus={() => props.setActive(index)}
            >
              <td className="firm-table__select">
                <SelectBox row={row} {...props} />
              </td>
              <td>
                <ClientCell row={row} index={index} {...props} />
              </td>
              <td>
                <BookedUntil row={row} />
              </td>
              <td className="ui-num">
                <Count value={row.counts.to_book} />
                {row.counts.auto_bookings > 0 ? (
                  <span className="firm-cell-note">
                    {t("client.todo.row.proposed", { count: row.counts.auto_bookings })}
                  </span>
                ) : null}
              </td>
              <td className="ui-num">
                <Count value={row.counts.missing_receipts} />
              </td>
              <td className="ui-num">
                <Count value={row.counts.bank_to_match} />
              </td>
              <td className="ui-num">
                <Count value={row.counts.open_questions} />
              </td>
              <td>
                <VatCell row={row} />
              </td>
              <td className="ui-num">
                <DueCell row={row} />
              </td>
              <td>
                <RowMenu row={row} {...props} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The phone layout: one card per client, the same facts in reading order (triage). */
function CardList(props: RowsProps) {
  const { t } = useI18n();
  const { rows } = props;
  return (
    <ul className="firm-cards" data-testid="firm-worklist-cards">
      {rows.map((row, index) => (
        <li
          key={row.administration_id}
          className={index === props.active ? "firm-card firm-card--active" : "firm-card"}
          data-testid={`firm-card-${row.administration_id}`}
          onFocus={() => props.setActive(index)}
        >
          <div className="firm-card__head">
            <SelectBox row={row} {...props} />
            <ClientCell row={row} index={index} {...props} />
            <RowMenu row={row} {...props} />
          </div>
          <dl className="firm-card__facts">
            <div>
              <dt>{t("client.todo.col.booked_until")}</dt>
              <dd>
                <BookedUntil row={row} />
              </dd>
            </div>
            <div>
              <dt>{t("client.todo.col.to_book")}</dt>
              <dd className="ui-num">
                <Count value={row.counts.to_book} />
              </dd>
            </div>
            <div>
              <dt>{t("client.todo.col.missing_receipts")}</dt>
              <dd className="ui-num">
                <Count value={row.counts.missing_receipts} />
              </dd>
            </div>
            <div>
              <dt>{t("client.todo.col.bank")}</dt>
              <dd className="ui-num">
                <Count value={row.counts.bank_to_match} />
              </dd>
            </div>
            <div>
              <dt>{t("client.todo.col.questions")}</dt>
              <dd className="ui-num">
                <Count value={row.counts.open_questions} />
              </dd>
            </div>
            <div>
              <dt>{t("client.todo.col.vat")}</dt>
              <dd>
                <VatCell row={row} />
                {row.vat !== null ? (
                  <>
                    {" "}
                    <DueCell row={row} />
                  </>
                ) : null}
              </dd>
            </div>
          </dl>
        </li>
      ))}
    </ul>
  );
}

function SelectBox({ row, selected, onToggle }: { row: FirmWorklistRowView } & RowsProps) {
  const { t } = useI18n();
  return (
    <input
      type="checkbox"
      className="firm-select"
      aria-label={t("client.todo.row.select", { name: row.display_name })}
      checked={selected.has(row.administration_id)}
      data-testid={`firm-select-${row.administration_id}`}
      onChange={() => onToggle(row.administration_id)}
    />
  );
}

function ClientCell({
  row,
  index,
  openRefs,
  onOpen,
  openingId,
  assigned,
}: { row: FirmWorklistRowView; index: number } & RowsProps) {
  const { t, date } = useI18n();
  return (
    <div className="firm-client">
      <span className={`client-marker client-marker--${row.colour}`} aria-hidden="true">
        {row.initials}
      </span>
      <div className="firm-client__text">
        <button
          type="button"
          ref={(element) => {
            openRefs.current[index] = element;
          }}
          className="firm-client__name"
          disabled={openingId !== null}
          data-testid={`firm-open-${row.administration_id}`}
          onClick={() => void onOpen(row)}
        >
          {openingId === row.administration_id ? t("client.portfolio.opening") : row.display_name}
        </button>
        {row.broken_feed !== null ? (
          <span className="firm-negative firm-cell-line" data-testid="firm-row-feed">
            <TriangleAlert size={14} aria-hidden="true" />
            {row.broken_feed.status === "expired"
              ? t("client.todo.row.feed_expired", { bank: row.broken_feed.bank_name })
              : t("client.todo.row.feed_failed", { bank: row.broken_feed.bank_name })}
          </span>
        ) : null}
        {row.snoozed_until !== null ? (
          <span className="firm-muted firm-cell-line">
            {t("client.todo.row.snoozed_until", { date: date(row.snoozed_until) })}
            {row.snooze_reason ? ` · ${row.snooze_reason}` : null}
          </span>
        ) : row.waiting_on_client_since !== null ? (
          <span className="firm-muted firm-cell-line">
            {t("client.todo.row.waiting_since", { date: date(row.waiting_on_client_since) })}
          </span>
        ) : null}
        {assigned === "any" ? (
          <span className="firm-muted firm-cell-line">
            {row.assigned_name !== null
              ? t("client.todo.row.assigned_to", { name: row.assigned_name })
              : t("client.todo.row.unassigned")}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function Count({ value }: { value: number }) {
  if (value > 0) return <span className="firm-count">{value}</span>;
  return <Dash />;
}

function Dash() {
  const { t } = useI18n();
  return (
    <span className="firm-zero">
      <span aria-hidden="true">–</span>
      <span className="ledgr-visually-hidden">{t("client.todo.row.none")}</span>
    </span>
  );
}

function BookedUntil({ row }: { row: FirmWorklistRowView }) {
  const { t, date } = useI18n();
  return (
    <span className="firm-booked">
      <span className="ui-num">
        {row.booked_until !== null ? date(row.booked_until) : t("client.todo.row.nothing_booked")}
      </span>
      {row.months_behind > 1 ? (
        <span className="firm-behind" data-testid="firm-row-behind">
          {t("client.todo.row.months_behind", { count: row.months_behind })}
        </span>
      ) : null}
      {row.booked_until_capped_by_feed ? (
        <span className="firm-cell-note">{t("client.todo.row.capped_by_feed")}</span>
      ) : null}
    </span>
  );
}

function VatCell({ row }: { row: FirmWorklistRowView }) {
  const { t } = useI18n();
  if (row.vat === null) return <Dash />;
  const variant = VAT_BADGE[row.vat.status] ?? "neutral";
  return (
    <span className="firm-vat">
      <span className="firm-vat__period">{row.vat.period_label}</span>
      {row.vat.status in VAT_BADGE ? (
        <Badge variant={variant}>{t(`client.todo.vat.${row.vat.status}`)}</Badge>
      ) : null}
    </span>
  );
}

function DueCell({ row }: { row: FirmWorklistRowView }) {
  const { t } = useI18n();
  if (row.vat === null || row.vat.status === "filed") return <Dash />;
  const days = row.vat.days_to_due;
  let text: ReactNode;
  if (days < 0) {
    text = (
      <Badge variant="overdue">{t("client.todo.due.overdue", { days: Math.abs(days) })}</Badge>
    );
  } else if (days === 0) {
    text = <strong>{t("client.todo.due.today")}</strong>;
  } else if (days < 7) {
    text = (
      <strong className="firm-due--soon">
        {t("client.todo.due.days", { days })}
        <span className="ledgr-visually-hidden"> {t("client.todo.due.soon")}</span>
      </strong>
    );
  } else {
    text = <span>{t("client.todo.due.days", { days })}</span>;
  }
  return (
    <span className="firm-due" data-testid="firm-row-due">
      {text}
    </span>
  );
}

function RowMenu({ row, onSnooze, onAssignRows }: { row: FirmWorklistRowView } & RowsProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const wrapper = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onPointer = (event: MouseEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  return (
    <div className="firm-menu" ref={wrapper}>
      <Button
        variant="ghost"
        size="sm"
        iconOnly
        aria-label={t("client.todo.row.actions", { name: row.display_name })}
        aria-expanded={open}
        aria-controls={menuId}
        data-testid={`firm-row-menu-${row.administration_id}`}
        onClick={() => setOpen((value) => !value)}
      >
        <Ellipsis size={18} aria-hidden="true" />
      </Button>
      {open ? (
        <ul id={menuId} className="firm-menu__list">
          <li>
            <button
              type="button"
              data-testid={`firm-row-snooze-${row.administration_id}`}
              onClick={() => {
                setOpen(false);
                onSnooze(row);
              }}
            >
              {t("client.todo.snooze.action")}
            </button>
          </li>
          <li>
            <button
              type="button"
              data-testid={`firm-row-assign-${row.administration_id}`}
              onClick={() => {
                setOpen(false);
                onAssignRows([row]);
              }}
            >
              {t("client.todo.assign.action")}
            </button>
          </li>
        </ul>
      ) : null}
    </div>
  );
}

function Pager({
  page,
  pageSize,
  shown,
  total,
  onPage,
  onShowAll,
}: {
  page: number;
  pageSize: number;
  shown: number;
  total: number;
  onPage: (page: number) => void;
  onShowAll: () => void;
}) {
  const { t } = useI18n();
  const from = (page - 1) * pageSize + 1;
  const to = from + shown - 1;
  const last = Math.max(1, Math.ceil(total / pageSize));
  return (
    <nav className="firm-pager" aria-label={t("client.todo.table_caption")}>
      <span className="firm-muted ui-num" data-testid="firm-pager-showing">
        {t("client.todo.page.showing", { from, to, total })}
      </span>
      {last > 1 ? (
        <span className="firm-pager__buttons">
          <Button size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
            {t("client.todo.page.previous")}
          </Button>
          <Button size="sm" disabled={page >= last} onClick={() => onPage(page + 1)}>
            {t("client.todo.page.next")}
          </Button>
          <Button size="sm" variant="ghost" onClick={onShowAll} data-testid="firm-show-all">
            {t("client.todo.page.show_all", { total })}
          </Button>
        </span>
      ) : null}
    </nav>
  );
}
