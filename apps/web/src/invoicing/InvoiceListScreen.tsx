import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type {
  SalesInvoicePaymentStatus,
  SalesInvoiceStatus,
  SalesInvoiceSummaryView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";

type View = "all" | "overdue" | "draft";

/**
 * `/invoices` — every sales invoice of the open administration, drafts
 * first-class (a half-finished invoice is resumable, ADR-046 §3), with three
 * views over the one list (all / overdue / drafts, each counted) and the one
 * primary action: a new invoice.
 *
 * Each row answers what a bookkeeper scans a list for: where it stands with
 * the money (`payment_status` — the same "overdue" the ageing report and the
 * reminder ladder use), what it is worth, the PDF attached, how it was sent, and
 * when it was paid. Amounts are decimal strings from the server, passed to
 * `money()` untouched (NFR-031); `gross_amount` is null where the VAT cannot be
 * worked out, which shows as a dash rather than a wrong figure.
 *
 * A draft can be deleted from its row. It is a two-step control because the
 * discard cannot be undone; an issued invoice has no such control, since its
 * number belongs to a gapless series (FR-AR-004) and the server refuses anyway.
 */
export function InvoiceListScreen() {
  const { t, date, money } = useI18n();
  const navigate = useNavigate();
  const { administration } = useAdministration();
  const { invoices } = useServices();
  const [rows, setRows] = useState<readonly SalesInvoiceSummaryView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [view, setView] = useState<View>("all");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteProblem, setDeleteProblem] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setProblem(null);
    invoices
      .listInvoices(administration.id)
      .then((result) => {
        if (!cancelled) setRows(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [invoices, administration.id, attempt]);

  const counts = useMemo(
    () => ({
      overdue: (rows ?? []).filter((row) => row.payment_status === "overdue").length,
      draft: (rows ?? []).filter((row) => row.status === "draft").length,
    }),
    [rows],
  );
  const visible = useMemo(
    () =>
      (rows ?? []).filter((row) =>
        view === "overdue"
          ? row.payment_status === "overdue"
          : view === "draft"
            ? row.status === "draft"
            : true,
      ),
    [rows, view],
  );

  async function discard(invoiceId: string) {
    setDeleting(true);
    setDeleteProblem(null);
    try {
      await invoices.discardInvoice(administration.id, invoiceId);
      setRows((current) => (current ?? []).filter((row) => row.id !== invoiceId));
      setConfirming(null);
    } catch (error) {
      setDeleteProblem(describeError(error));
    } finally {
      setDeleting(false);
    }
  }

  const tabs: readonly { id: View; label: string }[] = [
    { id: "all", label: t("invoice.list.tab.all") },
    { id: "overdue", label: t("invoice.list.tab.overdue", { count: counts.overdue }) },
    { id: "draft", label: t("invoice.list.tab.draft", { count: counts.draft }) },
  ];

  return (
    <section className="screen" aria-label={t("invoice.list.title")} data-testid="invoice-list">
      <PageHeader
        title={t("invoice.list.title")}
        context={rows === null ? undefined : t("invoice.list.count", { count: rows.length })}
        action={
          <Link
            to="/invoices/new"
            className="button-link button-link--primary"
            data-testid="invoice-list-new"
          >
            <Icon name="plus" size={18} />
            {t("invoice.list.new")}
          </Link>
        }
      />

      {problem !== null ? (
        <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />
      ) : rows === null ? (
        <LoadingSkeleton rows={4} />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Icon name="invoice" size={32} />}
          title={t("invoice.list.empty_title")}
          body={t("invoice.list.empty_body")}
          action={
            <Link to="/invoices/new" className="button-link button-link--primary">
              {t("invoice.list.new")}
            </Link>
          }
        />
      ) : (
        <>
          <div className="tabs" role="tablist" aria-label={t("invoice.list.tabs")}>
            {tabs.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={view === tab.id}
                className="tabs__tab"
                data-testid={`invoice-tab-${tab.id}`}
                onClick={() => setView(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {deleteProblem !== null ? (
            <p role="alert" className="alert alert--attention" data-testid="invoice-delete-problem">
              {deleteProblem}
            </p>
          ) : null}

          {visible.length === 0 ? (
            <p className="caption" data-testid="invoice-view-empty">
              {t("invoice.list.empty_view")}
            </p>
          ) : (
            <div className="panel table-wrap" role="tabpanel">
              <table className="table" data-testid="invoice-table">
                <thead>
                  <tr>
                    <th scope="col">{t("invoice.list.column.status")}</th>
                    <th scope="col">{t("invoice.list.column.date")}</th>
                    <th scope="col">{t("invoice.list.column.number")}</th>
                    <th scope="col">{t("invoice.list.column.customer")}</th>
                    <th scope="col" className="table__num">
                      {t("invoice.list.column.amount")}
                    </th>
                    <th scope="col">{t("invoice.list.column.attachment")}</th>
                    <th scope="col">{t("invoice.list.column.due")}</th>
                    <th scope="col">{t("invoice.list.column.send_method")}</th>
                    <th scope="col">{t("invoice.list.column.payment_date")}</th>
                    <th scope="col">
                      <span className="ledgr-visually-hidden">
                        {t("invoice.list.column.actions")}
                      </span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((invoice) => (
                    <tr key={invoice.id} data-testid="invoice-row">
                      <td>
                        <PaymentStatusChip status={invoice.payment_status} />
                      </td>
                      <td className="ledgr-num">{date(invoice.invoice_date)}</td>
                      <td className="ledgr-num">
                        <button
                          type="button"
                          className="table__row-button"
                          data-testid={`invoice-open-${invoice.id}`}
                          onClick={() => navigate(`/invoices/${encodeURIComponent(invoice.id)}`)}
                        >
                          {invoice.invoice_reference ?? t("invoice.list.draft_reference")}
                        </button>
                      </td>
                      <td>{invoice.customer_name}</td>
                      <td className="table__num" data-testid="invoice-amount">
                        {invoice.gross_amount === null ? "—" : money(invoice.gross_amount)}
                      </td>
                      <td className="table__muted">{invoice.attachment_name ?? "—"}</td>
                      <td className="ledgr-num table__muted">
                        {invoice.due_date === null ? "—" : date(invoice.due_date)}
                      </td>
                      <td data-testid="invoice-send-method">
                        <SendMethod invoice={invoice} />
                      </td>
                      <td className="ledgr-num table__muted" data-testid="invoice-payment-date">
                        {invoice.payment_date === null ? "—" : date(invoice.payment_date)}
                      </td>
                      <td className="table__actions">
                        {invoice.status !== "draft" ? null : confirming === invoice.id ? (
                          <span className="form__actions">
                            <button
                              type="button"
                              className="table__link-button table__link-button--danger"
                              disabled={deleting}
                              data-testid={`invoice-delete-confirm-${invoice.id}`}
                              onClick={() => void discard(invoice.id)}
                            >
                              {t("invoice.list.delete_confirm")}
                            </button>
                            <button
                              type="button"
                              className="table__link-button"
                              disabled={deleting}
                              onClick={() => setConfirming(null)}
                            >
                              {t("common.action.cancel")}
                            </button>
                          </span>
                        ) : (
                          <button
                            type="button"
                            className="table__link-button"
                            data-testid={`invoice-delete-${invoice.id}`}
                            onClick={() => {
                              setDeleteProblem(null);
                              setConfirming(invoice.id);
                            }}
                          >
                            {t("invoice.list.delete")}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}

const PAYMENT_CHIP: Record<SalesInvoicePaymentStatus, string> = {
  draft: "chip chip--dot",
  credit_note: "chip chip--dot",
  open: "chip chip--dot chip--accent",
  overdue: "chip chip--dot chip--attention",
  partially_paid: "chip chip--dot chip--caution",
  paid: "chip chip--dot chip--positive",
  settled: "chip chip--dot",
};

/** The receivables state as a word and a dot: never colour alone. */
function PaymentStatusChip({ status }: { status: SalesInvoicePaymentStatus }) {
  const { t } = useI18n();
  return (
    <span className={PAYMENT_CHIP[status]} data-testid="invoice-status" data-status={status}>
      {t(`invoice.list.payment_status.${status}`)}
    </span>
  );
}

/** How the latest delivery went out. A draft has none to show; an issued invoice never sent says so. */
function SendMethod({ invoice }: { invoice: SalesInvoiceSummaryView }) {
  const { t } = useI18n();
  if (invoice.send_channel === null) {
    return invoice.status === "draft" ? (
      <span className="table__muted">—</span>
    ) : (
      <span className="table__muted">{t("invoice.list.send.not_sent")}</span>
    );
  }
  const channel = t(`invoice.list.send.${invoice.send_channel}`);
  if (invoice.send_status === "bounced" || invoice.send_status === "failed") {
    return (
      <span className="chip chip--caution" data-testid="invoice-send-problem-chip">
        {t("invoice.list.send.problem", { channel })}
      </span>
    );
  }
  return (
    <span title={channel}>
      {invoice.send_channel === "email" ? <Icon name="mail" size={16} /> : null} {channel}
    </span>
  );
}

export function InvoiceStatusChip({ status }: { status: SalesInvoiceStatus }) {
  const { t } = useI18n();
  return (
    <span
      className={status === "issued" ? "chip chip--positive" : "chip chip--caution"}
      data-testid="invoice-status"
    >
      {t(`mobile.view.invoice_status.${status}`)}
    </span>
  );
}
