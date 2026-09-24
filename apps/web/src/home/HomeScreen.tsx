import { useEffect, useState } from "react";
import { Camera, Check, CircleAlert, Plus } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type {
  DashboardActionItemView,
  DashboardView,
  SalesInvoiceSummaryView,
} from "@ledgr/shared-types";

import "./HomeScreen.css";
import type { SalesInvoiceApi } from "../invoicing/api";
import { ErrorState } from "../shell/ScreenState";
import {
  Amount,
  Badge,
  Button,
  Card,
  CardHead,
  CashChart,
  EmptyState,
  KpiCard,
  Row,
  useMoney,
  type BadgeVariant,
} from "../ui";
import type { DashboardApi } from "./api";

/**
 * FR-UX-005 / MOB-006: the home screen, drawn as design/reference/Home.png.
 *
 *     FR-UX-005  The home screen is a prioritised list of what needs the
 *                user's attention, not a menu of everything the product can do.
 *     MOB-006    Dashboard: cash position, receivables, VAT estimate, items
 *                needing attention.
 *
 * --- What is real and what is left out (ADR-080) ---
 *
 * Every figure here comes from `GET .../dashboard` or the invoice list. The
 * reference also shows things that are drawn only when the API supplies them,
 * and left out rather than invented when it does not:
 *
 *   - the change on last month (`cash_change`) and the cash chart
 *     (`cash_history`, at least two months inside the fiscal year)
 *   - the BTW period, due date and days left (`vat_return`). The rule lives in
 *     `api.vat.deadlines`, so no statutory date is worked out in the browser
 *   - the amount on a draft invoice (nothing is owed on it yet). A draft receipt
 *     shows its gross and an overdue invoice what is still outstanding
 *   - the bank step of the setup list, which has no screen yet
 *
 * The API has already ordered the attention list (FR-UX-005's ranking is the
 * backend's job) and this component does not re-sort it.
 *
 * --- Two states ---
 *
 * A company with no bookings yet (all three figures zero and nothing needing
 * attention) gets the "Set up" checklist instead of the attention list, so the
 * first thing a new company sees is what to do next. Everything else is the
 * populated home.
 */
export function HomeScreen({
  administrationId,
  fiscalYearId,
  api,
  invoicesApi,
  companyName,
  onOpenItem,
  onNavigate = () => undefined,
}: {
  administrationId: string;
  fiscalYearId: string;
  api: DashboardApi;
  /** For the recent-invoices card. Omitted or failing, the card is simply absent. */
  invoicesApi?: SalesInvoiceApi;
  companyName?: string;
  onOpenItem: (item: DashboardActionItemView) => void;
  /** Where the header actions and the setup steps go; the route owns the URLs. */
  onNavigate?: (path: string) => void;
}) {
  const { t, language, date } = useI18n();
  const money = useMoney();
  const [summary, setSummary] = useState<DashboardView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [invoices, setInvoices] = useState<readonly SalesInvoiceSummaryView[]>([]);

  useEffect(() => {
    let cancelled = false;
    setSummary(null);
    setProblem(null);
    api
      .getDashboard(administrationId, fiscalYearId)
      .then((result) => {
        if (!cancelled) setSummary(result);
      })
      .catch((error: unknown) => {
        if (!cancelled)
          setProblem(error instanceof Error ? error.message : t("mobile.common.error"));
      });
    return () => {
      cancelled = true;
    };
    // `t` is stable per language and the message it produces is a fallback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [administrationId, fiscalYearId, api, attempt]);

  useEffect(() => {
    if (!invoicesApi) return;
    let cancelled = false;
    invoicesApi
      .listInvoices(administrationId)
      .then((rows) => {
        if (!cancelled) setInvoices(rows);
      })
      .catch(() => {
        // Secondary content: no card is better than an error on top of a
        // working dashboard. The invoice list screen reports its own failures.
      });
    return () => {
      cancelled = true;
    };
  }, [administrationId, invoicesApi]);

  const items = summary?.items_needing_action ?? [];
  const isSetup =
    summary !== null &&
    items.length === 0 &&
    isZero(summary.cash_position) &&
    isZero(summary.receivables) &&
    isZero(summary.vat_estimate);
  const history = summary?.cash_history ?? [];
  const change = summary?.cash_change ?? null;
  const cashChip =
    !isSetup && change !== null && !isZero(change)
      ? `${change.startsWith("-") ? "↓" : "↑"} ${money(change.replace(/^-/, ""))}`
      : null;
  const previousMonth =
    history.length >= 2
      ? monthName(history[history.length - 2]?.month ?? "", language, "long")
      : "";
  const vatReturn = summary?.vat_return ?? null;
  const periodStart = vatReturn?.period_start ?? summary?.vat_period_start ?? "";
  const periodEnd = vatReturn?.period_end ?? summary?.vat_period_end ?? "";
  const daysLeft = vatReturn ? daysUntil(vatReturn.due_date) : null;
  const draftReceipts =
    summary?.receipts_to_review ?? items.filter((item) => item.kind === "draft_expense").length;
  const recent = [...invoices]
    .sort((a, b) => b.invoice_date.localeCompare(a.invoice_date))
    .slice(0, 4);

  return (
    <section className="home" aria-label={t("mobile.home.title")}>
      <div className="home__top">
        <div>
          <h1 className="home__title">{greeting(t)}</h1>
          <p className="home__date" data-testid="home-today">
            {longToday(language)}
          </p>
        </div>
        <div className="home__actions">
          <Button onClick={() => onNavigate("/purchases")}>
            <Camera size={18} strokeWidth={1.8} aria-hidden="true" />
            {t("mobile.home.action.capture_receipt")}
          </Button>
          <Button variant="primary" onClick={() => onNavigate("/invoices/new")}>
            <Plus size={18} strokeWidth={2.2} aria-hidden="true" />
            {t("mobile.home.action.new_invoice")}
          </Button>
        </div>
      </div>

      {problem !== null ? (
        <ErrorState
          message={problem}
          onRetry={() => setAttempt((n) => n + 1)}
          testId="home-error"
        />
      ) : null}

      {/*
        A skeleton rather than a line of text, so the page does not jump when
        the data lands. `role="status"` keeps the announcement; the visible
        text stays in the tree for anyone who needs it, hidden from sight only.
      */}
      {summary === null && problem === null ? (
        <div role="status" data-testid="home-loading" className="home__loading">
          <span className="ledgr-visually-hidden">{t("mobile.common.loading")}</span>
          <div className="skeleton home__loading-row" />
          <div className="skeleton home__loading-row" />
          <div className="skeleton home__loading-row" />
        </div>
      ) : null}

      {summary !== null ? (
        <>
          <section
            className="home__kpis"
            aria-label={t("mobile.home.figures_heading")}
            data-testid="home-figures"
          >
            <KpiCard
              label={t("mobile.home.cash_position_label")}
              value={<span data-testid="home-cash-position">{money(summary.cash_position)}</span>}
              chip={cashChip}
              caption={
                <>
                  {cashChip ? (
                    <>
                      {t("mobile.home.cash_vs", { month: previousMonth })}
                      <br />
                    </>
                  ) : null}
                  <span data-testid="home-cash-position-caption">
                    {t(
                      isSetup ? "mobile.home.kpi.cash_empty" : "mobile.home.cash_position_caption",
                    )}
                  </span>
                </>
              }
            />
            <KpiCard
              label={t("mobile.home.receivables_label")}
              value={<span data-testid="home-receivables">{money(summary.receivables)}</span>}
              caption={
                <span data-testid="home-receivables-caption">
                  {t(
                    isSetup
                      ? "mobile.home.kpi.receivables_empty"
                      : "mobile.home.receivables_caption",
                  )}
                </span>
              }
            />
            <KpiCard
              label={t("mobile.home.vat_estimate_label")}
              value={<span data-testid="home-vat-estimate">{money(summary.vat_estimate)}</span>}
              caption={
                <>
                  <span data-testid="home-vat-estimate-period">
                    {t("mobile.home.vat_estimate_period", {
                      start: date(summary.vat_period_start),
                      end: date(summary.vat_period_end),
                    })}
                  </span>
                  {" · "}
                  <span data-testid="home-vat-estimate-caption">
                    {t(isSetup ? "mobile.home.kpi.vat_empty" : "mobile.home.vat_estimate_caption")}
                  </span>
                  <br />
                  <button
                    type="button"
                    className="ui-textbutton"
                    data-testid="home-vat-open"
                    onClick={() => onNavigate("/vat")}
                  >
                    {t("vat.open_from_home")}
                  </button>
                </>
              }
            />
          </section>

          <div className="home__columns">
            <div className="home__column">
              {isSetup ? (
                <SetupCard companyName={companyName} onNavigate={onNavigate} />
              ) : (
                <Card aria-label={t("mobile.home.items_heading")} className="home__clip">
                  <CardHead title={t("mobile.home.items_heading")}>
                    {items.length > 0 ? (
                      <span data-testid="home-item-count">
                        <Badge
                          variant={
                            items.some((i) => i.kind === "overdue_invoice") ? "overdue" : "draft"
                          }
                        >
                          {t("mobile.home.item_count", { count: items.length })}
                        </Badge>
                      </span>
                    ) : null}
                  </CardHead>

                  {items.length === 0 ? (
                    <div className="home__caught-up">
                      <EmptyState
                        testId="home-items-empty"
                        icon={<Check size={24} strokeWidth={1.5} aria-hidden="true" />}
                        title={t("mobile.home.items_empty_title")}
                      >
                        {t("mobile.home.items_empty")}
                      </EmptyState>
                    </div>
                  ) : (
                    <ul
                      className="ui-list"
                      aria-label={t("mobile.home.items_heading")}
                      data-testid="home-items-list"
                    >
                      {items.map((item) => (
                        <li key={item.id} data-testid="home-item">
                          <Row height={72} className="home__list-row">
                            <Badge variant={badgeFor(item.kind)} className="home__item-badge">
                              {t(`mobile.home.state.${item.kind}`)}
                            </Badge>
                            <button
                              type="button"
                              className="home__item-row"
                              data-testid={`home-item-${item.id}`}
                              onClick={() => onOpenItem(item)}
                            >
                              {/* Already translated server-side (FR-UX-007),
                                  placed as-is rather than recomposed from `kind`. */}
                              {item.description}
                            </button>
                            <span className="home__item-amount">
                              {item.amount ? <Amount value={item.amount} /> : null}
                            </span>
                            <Button
                              size="sm"
                              className="home__item-action"
                              data-testid={`home-item-action-${item.id}`}
                              onClick={() => onOpenItem(item)}
                            >
                              {t(`mobile.home.action.${item.kind}`)}
                            </Button>
                          </Row>
                        </li>
                      ))}
                    </ul>
                  )}
                </Card>
              )}

              {!isSetup && recent.length > 0 ? (
                <Card aria-label={t("mobile.home.recent.title")} className="home__clip">
                  <CardHead title={t("mobile.home.recent.title")} />
                  <ul className="ui-list" data-testid="home-recent">
                    {recent.map((invoice) => (
                      <li key={invoice.id}>
                        <Row height={51}>
                          <span className="home__recent-text">
                            {invoice.invoice_reference ?? t("invoice.list.draft_reference")}
                            {" · "}
                            {invoice.customer_name}
                          </span>
                          <Badge variant={invoice.status === "draft" ? "draft" : "neutral"}>
                            {t(`mobile.view.invoice_status.${invoice.status}`)}
                          </Badge>
                          <span className="home__recent-date">{date(invoice.invoice_date)}</span>
                        </Row>
                      </li>
                    ))}
                  </ul>
                </Card>
              ) : null}
            </div>

            <div className="home__column">
              {isSetup ? (
                <Card padded aria-label={t("mobile.home.cash_position_label")}>
                  <h2 className="ui-card__title">{t("mobile.home.cash_position_label")}</h2>
                  <div className="home__chart-empty">
                    <EmptyState
                      testId="home-chart-empty"
                      title={t("mobile.home.chart.empty_title")}
                    >
                      {t("mobile.home.chart.empty_body")}
                    </EmptyState>
                  </div>
                </Card>
              ) : history.length >= 2 ? (
                <Card padded aria-label={t("mobile.home.cash_position_label")}>
                  <div className="home__chart-head">
                    <h2 className="ui-card__title">{t("mobile.home.cash_position_label")}</h2>
                    <span className="home__chart-note">
                      {t("mobile.home.chart.months", { count: history.length })}
                    </span>
                  </div>
                  <div className="home__chart-empty">
                    <CashChart
                      series={history.map((point) => ({
                        label: monthName(point.month, language, "short"),
                        value: point.balance,
                      }))}
                      summary={t("mobile.home.chart.summary", {
                        from: money(history[0]?.balance ?? "0"),
                        fromMonth: monthName(history[0]?.month ?? "", language, "long"),
                        to: money(history[history.length - 1]?.balance ?? "0"),
                        toMonth: monthName(
                          history[history.length - 1]?.month ?? "",
                          language,
                          "long",
                        ),
                      })}
                    />
                  </div>
                </Card>
              ) : null}

              <Card padded aria-label={t("mobile.home.btw.title")}>
                <div className="home__btw-head">
                  <h2 className="ui-card__title">{t("mobile.home.btw.title")}</h2>
                  {daysLeft !== null ? (
                    <Badge variant="draft" data-testid="home-btw-days">
                      {t("mobile.home.btw.days", { count: daysLeft })}
                    </Badge>
                  ) : null}
                </div>
                <p className="home__btw-period" data-testid="home-btw-period">
                  {periodLabel(periodStart, periodEnd) ??
                    t("mobile.home.vat_estimate_period", {
                      start: date(periodStart),
                      end: date(periodEnd),
                    })}
                  {vatReturn ? (
                    <span className="home__btw-due" data-testid="home-btw-due">
                      {t("mobile.home.btw.due", { date: date(vatReturn.due_date) })}
                    </span>
                  ) : null}
                </p>
                <p className="home__btw-estimate">
                  <Amount value={vatReturn?.estimate ?? summary.vat_estimate} />{" "}
                  {t("mobile.home.btw.estimated")}
                </p>
                {isSetup ? (
                  <p className="home__btw-note">{t("mobile.home.btw.empty")}</p>
                ) : draftReceipts > 0 ? (
                  <p className="home__btw-note">
                    <CircleAlert size={14} strokeWidth={1.8} aria-hidden="true" />{" "}
                    {t("mobile.home.btw.receipts_to_review", { count: draftReceipts })}
                  </p>
                ) : null}
              </Card>
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}

/**
 * The empty home's checklist (Home-empty.png): what to do to reach the first
 * booked entry. "Company details" is always done, since the screen only exists
 * inside an administration. The first pending step wears the one primary button.
 *
 * The reference's second step, connecting a bank account, has no screen in the
 * product yet, so it is replaced by adding a customer, which does.
 */
function SetupCard({
  companyName,
  onNavigate,
}: {
  companyName?: string;
  onNavigate: (path: string) => void;
}) {
  const { t } = useI18n();
  const steps: ReadonlyArray<{ id: string; path: string | null }> = [
    { id: "company", path: null },
    { id: "customer", path: "/customers/new" },
    { id: "invoice", path: "/invoices/new" },
    { id: "capture", path: "/purchases" },
  ];
  const firstPending = steps.findIndex((step) => step.path !== null);

  return (
    <Card aria-label={t("mobile.home.setup.aria")} className="home__clip" data-testid="home-setup">
      <div className="home__setup-head">
        <h2 className="ui-card__title">
          {t("mobile.home.setup.title", {
            company: companyName ?? t("mobile.home.setup.your_company"),
          })}
        </h2>
        <p className="home__setup-sub">{t("mobile.home.setup.subtitle")}</p>
      </div>
      <ol className="ui-list">
        {steps.map((step, index) => (
          <li key={step.id}>
            <Row height={82}>
              <span
                className={`home__step-mark${step.path === null ? " home__step-mark--done" : ""}`}
                aria-hidden="true"
              >
                {step.path === null ? <Check size={16} strokeWidth={2.2} /> : index + 1}
              </span>
              <span className="home__step-text">
                <span className="home__step-title">
                  {t(`mobile.home.setup.step.${step.id}.title`)}
                </span>
                <span className="home__step-body">
                  {t(`mobile.home.setup.step.${step.id}.body`)}
                </span>
              </span>
              {step.path === null ? (
                <Badge variant="booked">{t("mobile.home.setup.done")}</Badge>
              ) : (
                <Button
                  size="sm"
                  variant={index === firstPending ? "primary" : "secondary"}
                  data-testid={`home-setup-${step.id}`}
                  onClick={() => onNavigate(step.path as string)}
                >
                  {t(`mobile.home.setup.step.${step.id}.action`)}
                </Button>
              )}
            </Row>
          </li>
        ))}
      </ol>
    </Card>
  );
}

/** A decimal string that is zero: "0", "0.00", "-0.00". Compared as text, never as a float. */
function isZero(amount: string): boolean {
  return /^[+-]?0*(\.0*)?$/.test(amount.trim());
}

function badgeFor(kind: DashboardActionItemView["kind"]): BadgeVariant {
  return kind === "overdue_invoice" ? "overdue" : "draft";
}

/** Time-of-day greeting from the browser clock: a display nicety, never a posting date. */
function greeting(t: ReturnType<typeof useI18n>["t"]): string {
  const hour = new Date().getHours();
  const key =
    hour < 12
      ? "mobile.home.greeting.morning"
      : hour < 18
        ? "mobile.home.greeting.afternoon"
        : "mobile.home.greeting.evening";
  return t(key);
}

/** "April" or "Apr" for a "YYYY-MM" month, in the reader's language. */
function monthName(month: string, language: string, style: "long" | "short"): string {
  const [year, number] = month.split("-").map(Number);
  if (!year || !number) return "";
  // en-US, not en-GB: en-GB abbreviates September as "Sept", the reference says "Sep".
  return new Intl.DateTimeFormat(language === "nl" ? "nl-NL" : "en-US", {
    month: style,
    timeZone: "UTC",
  })
    .format(new Date(Date.UTC(year, number - 1, 1)))
    .replace(".", "");
}

/** Whole days from today (the reader's calendar day) to an ISO date; display only. */
function daysUntil(isoDate: string): number | null {
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!parts) return null;
  const now = new Date();
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  const due = Date.UTC(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3]));
  return Math.round((due - today) / 86_400_000);
}

/** "Saturday 19 September 2026", in the reader's language. */
function longToday(language: string): string {
  return new Intl.DateTimeFormat(language === "nl" ? "nl-NL" : "en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  })
    .format(new Date())
    .replace(",", "");
}

/**
 * "Q3 2026" when the estimate covers exactly one calendar quarter, else null and
 * the caller shows the dates. Read off the ISO strings; no date arithmetic.
 */
function periodLabel(start: string, end: string): string | null {
  const s = /^(\d{4})-(\d{2})-01$/.exec(start);
  const e = /^(\d{4})-(\d{2})-(\d{2})$/.exec(end);
  if (!s || !e || s[1] !== e[1]) return null;
  const first = Number(s[2]);
  const quarterEnds: Record<number, [string, string]> = {
    1: ["03", "31"],
    4: ["06", "30"],
    7: ["09", "30"],
    10: ["12", "31"],
  };
  const expected = quarterEnds[first];
  if (!expected || e[2] !== expected[0] || e[3] !== expected[1]) return null;
  return `Q${(first - 1) / 3 + 1} ${s[1]}`;
}
