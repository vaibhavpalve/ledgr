import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Check as CheckIcon, ChevronDown, ChevronRight, Copy } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { VatBoxLineView, VatBoxView, VatCheckView, VatReturnView } from "@ledgr/shared-types";

import "./Vat.css";
import { ApiError, describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { Badge, useMoney, type BadgeVariant } from "../ui";
import { useModalFocus } from "../useModalFocus";

/**
 * `/vat` and `/vat/:periodId` — the BTW return (ADR-087).
 *
 * The overview lists every period of the fiscal year with what it comes to and when it is due.
 * One period opens to the return itself: the rubrieken as the Belastingdienst's form has them,
 * each drillable to the postings behind it (FR-VAT-011), the pre-filing checks with a way to fix
 * each (FR-VAT-002), and the filing step. Filing is manual today — the figures are entered in
 * Mijn Belastingdienst Zakelijk — so the screen is built to make that exact: a copy button per
 * figure, the steps written out, and the irreversible lock behind a confirmation.
 */
export function VatRoute() {
  const { periodId } = useParams();
  return periodId === undefined ? <VatOverview /> : <VatReturnDetail periodId={periodId} />;
}

// ---------------------------------------------------------------------------
// Shared wording
// ---------------------------------------------------------------------------

type UiStatus = "filed" | "ready" | "running" | "attention" | "empty";

/** Nothing in any box: nothing was booked with BTW in the period. */
function isEmpty(row: VatReturnView): boolean {
  return row.boxes.every(
    (box) => Number(box.turnover ?? "0") === 0 && Number(box.vat ?? "0") === 0,
  );
}

function uiStatus(row: VatReturnView): UiStatus {
  if (row.status === "filed") return "filed";
  if (row.checks.some((check) => check.code === "period_not_ended")) return "running";
  // A past period with nothing booked is not an overdue return shouting in red: a business
  // that started in September has nothing for January. It can still be opened and filed as nil.
  if (isEmpty(row)) return "empty";
  return row.can_be_filed ? "ready" : "attention";
}

const STATUS_BADGE: Record<UiStatus, BadgeVariant> = {
  filed: "booked",
  ready: "review",
  running: "neutral",
  attention: "overdue",
  empty: "neutral",
};

function parseDay(iso: string): Date {
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(year ?? 1970, (month ?? 1) - 1, day ?? 1);
}

function daysFromToday(iso: string): number {
  const today = new Date();
  const midnight = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((parseDay(iso).getTime() - midnight.getTime()) / 86_400_000);
}

/** A period's name as a person says it: "Q2 2026", "May 2026", or its dates. */
type PeriodLabeller = (row: Pick<VatReturnView, "start_date" | "end_date">) => string;

function usePeriodLabel(): PeriodLabeller {
  const { t, language, date } = useI18n();
  return (row) => {
    const start = parseDay(row.start_date);
    const end = parseDay(row.end_date);
    const months =
      (end.getFullYear() - start.getFullYear()) * 12 + end.getMonth() - start.getMonth() + 1;
    if (start.getDate() === 1 && months === 3 && start.getMonth() % 3 === 0) {
      return t("vat.period.quarter", {
        quarter: start.getMonth() / 3 + 1,
        year: start.getFullYear(),
      });
    }
    if (start.getDate() === 1 && months === 1) {
      const month = new Intl.DateTimeFormat(language === "nl" ? "nl-NL" : "en-GB", {
        month: "long",
        year: "numeric",
      }).format(start);
      return month.charAt(0).toUpperCase() + month.slice(1);
    }
    return t("vat.period.range", { start: date(row.start_date), end: date(row.end_date) });
  };
}

function DueText({ row }: { row: VatReturnView }) {
  const { t, date } = useI18n();
  const days = daysFromToday(row.due_date);
  const quiet = row.status === "filed" || uiStatus(row) === "empty";
  const relative = quiet
    ? null
    : days === 0
      ? t("vat.due_today")
      : days > 0
        ? t("vat.due_in", { days })
        : t("vat.overdue", { days: -days });
  return (
    <span className={`vat__due${days < 0 && !quiet ? " vat__due--late" : ""}`}>
      {t("vat.due", { date: date(row.due_date) })}
      {relative !== null ? ` · ${relative}` : null}
    </span>
  );
}

function TotalLabel({ total }: { total: string }) {
  const { t } = useI18n();
  const value = Number(total);
  if (value > 0) return <>{t("vat.to_pay")}</>;
  if (value < 0) return <>{t("vat.to_reclaim")}</>;
  return <>{t("vat.nothing_due")}</>;
}

function absolute(amount: string): string {
  return amount.startsWith("-") ? amount.slice(1) : amount;
}

// ---------------------------------------------------------------------------
// The overview
// ---------------------------------------------------------------------------

function VatOverview() {
  const { t } = useI18n();
  const money = useMoney();
  const periodLabel = usePeriodLabel();
  const { administration, fiscalYear } = useAdministration();
  const { vat } = useServices();
  const [rows, setRows] = useState<readonly VatReturnView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setProblem(null);
    vat
      .listReturns(administration.id, fiscalYear.id)
      .then((result) => {
        if (!cancelled) setRows(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [vat, administration.id, fiscalYear.id, attempt]);

  return (
    <section className="screen vat" aria-label={t("vat.title")} data-testid="vat-screen">
      <PageHeader
        title={t("vat.title")}
        context={t("ledger.fiscal_year_context", {
          start: fiscalYear.start_date.slice(0, 4),
          end: fiscalYear.end_date.slice(0, 4),
        })}
      />
      <p className="vat__intro">{t("vat.intro")}</p>
      {problem !== null ? (
        <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />
      ) : null}
      {rows === null && problem === null ? <LoadingSkeleton rows={4} /> : null}
      {rows !== null && rows.length === 0 ? (
        <EmptyState title={t("vat.empty_title")} body={t("vat.empty_body")} testId="vat-empty" />
      ) : null}
      {rows !== null && rows.length > 0 ? (
        <ul className="vat__periods" data-testid="vat-periods">
          {rows.map((row) => {
            const status = uiStatus(row);
            return (
              <li key={row.period_id}>
                <Link
                  to={`/vat/${row.period_id}`}
                  className={`vat__period vat__period--${status}`}
                  data-testid={`vat-period-${row.period_id}`}
                >
                  <span className="vat__period-head">
                    <span className="vat__period-name">{periodLabel(row)}</span>
                    <Badge variant={STATUS_BADGE[status]}>{t(`vat.status.${status}`)}</Badge>
                  </span>
                  <span className="vat__period-amount">
                    <span className="vat__period-label">
                      <TotalLabel total={row.total_due} />
                      {status === "running" ? ` · ${t("vat.estimate")}` : null}
                    </span>
                    <span className="vat__figure" data-testid="vat-period-total">
                      {money(absolute(row.total_due))}
                    </span>
                  </span>
                  <DueText row={row} />
                </Link>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// One return
// ---------------------------------------------------------------------------

function VatReturnDetail({ periodId }: { periodId: string }) {
  const { t, date } = useI18n();
  const money = useMoney();
  const periodLabel = usePeriodLabel();
  const { administration } = useAdministration();
  const { vat } = useServices();
  const [row, setRow] = useState<VatReturnView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setProblem(null);
    vat
      .getReturn(administration.id, periodId)
      .then((result) => {
        if (!cancelled) setRow(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [vat, administration.id, periodId, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  if (problem !== null && row === null) {
    return (
      <section className="screen vat" data-testid="vat-return">
        <Link to="/vat" className="button-link button-link--quiet">
          {t("vat.back")}
        </Link>
        <ErrorState message={problem} onRetry={reload} />
      </section>
    );
  }
  if (row === null) {
    return (
      <section className="screen vat" data-testid="vat-return">
        <LoadingSkeleton rows={8} />
      </section>
    );
  }

  const label = periodLabel(row);
  const status = uiStatus(row);
  const periodEnded = !row.checks.some((check) => check.code === "period_not_ended");

  return (
    <section className="screen vat" aria-label={label} data-testid="vat-return">
      <Link to="/vat" className="button-link button-link--quiet vat__back" data-testid="vat-back">
        {t("vat.back")}
      </Link>
      <PageHeader
        title={`${t("vat.title")} · ${label}`}
        context={t("vat.due", { date: date(row.due_date) })}
      />

      <div className="vat__summary" data-testid="vat-summary">
        <div className="vat__stat">
          <span className="vat__stat-label">{t("vat.summary.output")}</span>
          <span className="vat__figure">{money(row.output_vat)}</span>
        </div>
        <div className="vat__stat">
          <span className="vat__stat-label">{t("vat.summary.input")}</span>
          <span className="vat__figure">{money(row.input_vat)}</span>
        </div>
        <div className="vat__stat vat__stat--total">
          <span className="vat__stat-label">
            <TotalLabel total={row.total_due} />
            {status === "running" ? ` · ${t("vat.estimate")}` : null}
          </span>
          <span className="vat__figure vat__figure--large" data-testid="vat-total">
            {money(absolute(row.total_due))}
          </span>
          <Badge variant={STATUS_BADGE[status]}>{t(`vat.status.${status}`)}</Badge>
        </div>
      </div>

      {row.filed !== null ? (
        <div className="alert alert--positive" role="status" data-testid="vat-filed">
          <strong>{t("vat.filed.banner", { date: date(row.filed.filed_at.slice(0, 10)) })}</strong>{" "}
          {row.filed.filing_reference
            ? t("vat.filed.reference", { reference: row.filed.filing_reference })
            : t("vat.filed.no_reference")}
          <p>{t("vat.filed.locked")}</p>
        </div>
      ) : (
        <Checks checks={row.checks} periodEnd={row.end_date} />
      )}

      <BoxesTable row={row} canCopy={row.filed === null && periodEnded} />

      {row.filed === null && periodEnded ? (
        <FilePanel row={row} label={label} onFiled={setRow} />
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// FR-VAT-002: the checks, each with the place it is fixed
// ---------------------------------------------------------------------------

const FIX: Partial<Record<VatCheckView["code"], { to: string; key: string }>> = {
  unposted_purchases: { to: "/purchases", key: "vat.fix.purchases" },
  draft_sales_invoices: { to: "/invoices", key: "vat.fix.invoices" },
  unreconciled_bank: { to: "/bank", key: "vat.fix.bank" },
  untagged_vat_postings: { to: "/journal", key: "vat.fix.journal" },
  earlier_period_unfiled: { to: "/vat", key: "vat.fix.earlier" },
};

function Checks({ checks, periodEnd }: { checks: readonly VatCheckView[]; periodEnd: string }) {
  const { t, date } = useI18n();
  const money = useMoney();
  return (
    <div className="panel vat__checks" data-testid="vat-checks">
      <div className="panel__header">
        <h2>{t("vat.checks.title")}</h2>
      </div>
      {checks.length === 0 ? (
        <p className="vat__clean">
          <CheckIcon size={16} strokeWidth={2.2} aria-hidden="true" /> {t("vat.checks.clean")}
        </p>
      ) : (
        <ul className="vat__check-list">
          {checks.map((check) => {
            const fix = FIX[check.code];
            return (
              <li
                key={check.code}
                className={`vat__check vat__check--${check.severity}`}
                data-testid={`vat-check-${check.code}`}
              >
                <span
                  className={`chip ${check.severity === "blocking" ? "chip--attention" : "chip--caution"}`}
                >
                  {check.severity === "blocking" ? t("vat.blocking") : t("vat.warning")}
                </span>
                <span className="vat__check-text">
                  {t(`vat.check.${check.code}`, {
                    count: check.count ?? 0,
                    amount: check.amount !== null ? money(check.amount) : "",
                    detail: check.detail.join(", "),
                    end: date(periodEnd),
                  })}
                </span>
                {fix !== undefined ? (
                  <Link to={fix.to} className="button-link">
                    {t(fix.key)}
                  </Link>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The rubrieken, with FR-VAT-011's drill-down
// ---------------------------------------------------------------------------

function wholeEuros(amount: string | null): string {
  if (amount === null) return "";
  return amount.split(".")[0] ?? amount;
}

function CopyButton({ value, box }: { value: string; box: string }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="vat__copy"
      aria-label={t("vat.copy", { box })}
      title={copied ? t("vat.copied") : t("vat.copy", { box })}
      data-testid={`vat-copy-${box}`}
      onClick={() => {
        void navigator.clipboard
          ?.writeText(value)
          .then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          })
          .catch(() => undefined);
      }}
    >
      {copied ? (
        <CheckIcon size={14} strokeWidth={2.2} aria-hidden="true" />
      ) : (
        <Copy size={14} strokeWidth={1.8} aria-hidden="true" />
      )}
    </button>
  );
}

function BoxesTable({ row, canCopy }: { row: VatReturnView; canCopy: boolean }) {
  const { t, language } = useI18n();
  const money = useMoney();
  const [open, setOpen] = useState<string | null>(null);

  const cell = (box: VatBoxView, column: "turnover" | "vat") => {
    const rounded = column === "turnover" ? box.turnover_rounded : box.vat_rounded;
    const exact = column === "turnover" ? box.turnover : box.vat;
    if (rounded === null) return <td className="table__num vat__none" aria-hidden="true" />;
    return (
      <td className="table__num">
        <span className="vat__cell">
          <span title={exact !== null ? t("vat.exact", { amount: money(exact) }) : undefined}>
            {money(rounded)}
          </span>
          {canCopy ? (
            <CopyButton
              value={wholeEuros(rounded)}
              box={`${box.code} ${column === "vat" ? t("vat.column.vat") : t("vat.column.turnover")}`}
            />
          ) : null}
        </span>
      </td>
    );
  };

  return (
    <div className="panel table-wrap vat__boxes">
      <table className="table" data-testid="vat-boxes">
        <thead>
          <tr>
            <th scope="col">{t("vat.column.box")}</th>
            <th scope="col" className="table__num">
              {t("vat.column.turnover")}
            </th>
            <th scope="col" className="table__num">
              {t("vat.column.vat")}
            </th>
          </tr>
        </thead>
        <tbody>
          {row.boxes.map((box) => {
            const isOpen = open === box.code;
            const description =
              language === "en" && box.description_en ? box.description_en : box.description_nl;
            const summary = box.code === "5a" || box.code === "5b";
            return (
              <Fragment key={box.code}>
                <tr
                  className={`${summary ? "vat__row--summary" : ""}`}
                  data-testid={`vat-box-${box.code}`}
                >
                  <td className="ledgr-num">
                    <button
                      type="button"
                      className="table__row-button vat__toggle"
                      aria-expanded={isOpen}
                      aria-label={t("vat.drill.show", { box: box.code })}
                      data-testid={`vat-box-toggle-${box.code}`}
                      onClick={() => setOpen(isOpen ? null : box.code)}
                    >
                      {isOpen ? (
                        <ChevronDown size={14} aria-hidden="true" />
                      ) : (
                        <ChevronRight size={14} aria-hidden="true" />
                      )}
                      <strong>{box.code}</strong>
                    </button>
                    {/* One column for the box: its code and what it is, so the two amount
                        columns stay on screen at any width. */}
                    <span className="vat__desc-inline">{description}</span>
                  </td>
                  {cell(box, "turnover")}
                  {cell(box, "vat")}
                </tr>
                {isOpen ? (
                  <tr className="vat__drill-row">
                    <td colSpan={3}>
                      <BoxLines periodId={row.period_id} code={box.code} />
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <td>
              <strong>{t("vat.summary.total")}</strong>
            </td>
            <td />
            <td className="table__num">
              <span className="vat__cell">
                <strong>{money(row.total_due)}</strong>
              </span>
            </td>
          </tr>
        </tfoot>
      </table>
      <p className="caption vat__note">{t("vat.rounding_note")}</p>
      {row.exempt_turnover !== null && Number(row.exempt_turnover) !== 0 ? (
        <p className="caption vat__note">
          {t("vat.exempt_turnover", { amount: money(row.exempt_turnover) })}
        </p>
      ) : null}
    </div>
  );
}

function BoxLines({ periodId, code }: { periodId: string; code: string }) {
  const { t, date } = useI18n();
  const money = useMoney();
  const { administration } = useAdministration();
  const { vat } = useServices();
  const [lines, setLines] = useState<readonly VatBoxLineView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    vat
      .getBoxLines(administration.id, periodId, code)
      .then((result) => {
        if (!cancelled) setLines(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [vat, administration.id, periodId, code]);

  if (problem !== null) return <ErrorState message={problem} />;
  if (lines === null) return <LoadingSkeleton rows={2} />;
  if (lines.length === 0) return <p className="caption">{t("vat.drill.empty")}</p>;
  return (
    <div className="table-wrap">
      <table className="table vat__drill" data-testid={`vat-drill-${code}`}>
        <thead>
          <tr>
            <th scope="col">{t("vat.drill.column.entry")}</th>
            <th scope="col">{t("vat.drill.column.date")}</th>
            <th scope="col">{t("vat.drill.column.description")}</th>
            <th scope="col">{t("vat.drill.column.account")}</th>
            <th scope="col">{t("vat.drill.column.part")}</th>
            <th scope="col" className="table__num">
              {t("vat.drill.column.amount")}
            </th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line, index) => (
            <tr key={`${line.entry_id}-${index}`}>
              <td className="ledgr-num">{line.entry_number}</td>
              <td className="ledgr-num">{date(line.entry_date)}</td>
              <td>
                {line.description}
                {line.document_reference ? (
                  <span className="table__muted"> · {line.document_reference}</span>
                ) : null}
              </td>
              <td>
                <span className="ledgr-num">{line.account_code}</span> {line.account_name}
              </td>
              <td>{t(`vat.drill.part.${line.column}`)}</td>
              <td className="table__num">{money(line.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Filing
// ---------------------------------------------------------------------------

function FilePanel({
  row,
  label,
  onFiled,
}: {
  row: VatReturnView;
  label: string;
  onFiled: (row: VatReturnView) => void;
}) {
  const { t, date } = useI18n();
  const money = useMoney();
  const navigate = useNavigate();
  const { administration } = useAdministration();
  const { vat } = useServices();
  const warnings = row.checks.filter((check) => check.severity === "warning");
  const [reference, setReference] = useState("");
  const [acknowledged, setAcknowledged] = useState<ReadonlySet<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const allAcknowledged = warnings.every((warning) => acknowledged.has(warning.code));
  const ready = row.can_be_filed && allAcknowledged;

  const file = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const filed = await vat.fileReturn(administration.id, row.period_id, {
        filing_reference: reference.trim() === "" ? null : reference.trim(),
        acknowledged_warnings: warnings.map((warning) => warning.code),
        expected_total: row.total_due,
      });
      setConfirming(false);
      onFiled(filed);
      navigate(`/vat/${row.period_id}`, { replace: true });
    } catch (error) {
      setConfirming(false);
      setProblem(
        error instanceof ApiError && error.status === 403
          ? t("vat.file.not_permitted")
          : describeError(error),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel vat__file" data-testid="vat-file">
      <div className="panel__header">
        <h2>{t("vat.file.title")}</h2>
      </div>
      <div className="panel__body">
        <ol className="vat__steps">
          <li>{t("vat.file.step1", { period: label })}</li>
          <li>{t("vat.file.step2")}</li>
          <li>{t("vat.file.step3")}</li>
          <li>{t("vat.file.step4")}</li>
        </ol>
        {!row.can_be_filed ? (
          <p className="alert alert--attention">{t("vat.file.blocked")}</p>
        ) : (
          <div className="form">
            <div className="form__field">
              <label htmlFor="vat-reference">{t("vat.file.reference")}</label>
              <input
                id="vat-reference"
                type="text"
                value={reference}
                maxLength={100}
                autoComplete="off"
                data-testid="vat-reference"
                onChange={(event) => setReference(event.target.value)}
              />
            </div>
            {warnings.map((warning) => (
              <label key={warning.code} className="vat__ack">
                <input
                  type="checkbox"
                  checked={acknowledged.has(warning.code)}
                  data-testid={`vat-ack-${warning.code}`}
                  onChange={(event) => {
                    const next = new Set(acknowledged);
                    if (event.target.checked) next.add(warning.code);
                    else next.delete(warning.code);
                    setAcknowledged(next);
                  }}
                />
                <span>
                  <strong>
                    {t(`vat.check.${warning.code}`, {
                      count: warning.count ?? 0,
                      amount: warning.amount !== null ? money(warning.amount) : "",
                      detail: warning.detail.join(", "),
                      end: date(row.end_date),
                    })}
                  </strong>{" "}
                  {t("vat.file.acknowledge")}
                </span>
              </label>
            ))}
            {problem !== null ? (
              <p className="alert alert--attention" role="alert" data-testid="vat-file-error">
                {problem}
              </p>
            ) : null}
            <div className="form__actions">
              <button
                type="button"
                className="button--primary"
                disabled={!ready || busy}
                data-testid="vat-file-submit"
                onClick={() => setConfirming(true)}
              >
                {t("vat.file.submit")}
              </button>
            </div>
          </div>
        )}
      </div>
      {confirming ? (
        <ConfirmFiling
          label={label}
          busy={busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void file()}
        />
      ) : null}
    </div>
  );
}

function ConfirmFiling({
  label,
  busy,
  onCancel,
  onConfirm,
}: {
  label: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, dialogRef);
  return (
    <div className="dialog-backdrop">
      <div
        ref={dialogRef}
        className="dialog"
        role="alertdialog"
        aria-modal="true"
        aria-label={t("vat.file.confirm_title")}
        data-testid="vat-file-confirm"
      >
        <h2>{t("vat.file.confirm_title")}</h2>
        <p>{t("vat.file.confirm_body", { period: label })}</p>
        <div className="dialog__actions">
          <button type="button" onClick={onCancel} data-testid="vat-file-cancel">
            {t("vat.file.cancel")}
          </button>
          <button
            type="button"
            className="button--primary"
            disabled={busy}
            onClick={onConfirm}
            data-testid="vat-file-confirm-yes"
          >
            {t("vat.file.confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
