import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { SalesInvoiceStatus, SalesInvoiceSummaryView } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";

/**
 * `/invoices` — every sales invoice of the open administration, drafts
 * first-class (a half-finished invoice is resumable, ADR-046 §3), with a
 * status chip per row and the one primary action: a new invoice.
 *
 * The summary row (`_invoice_summary_json`) carries no amount — the per-
 * invoice reads are what the list endpoint deliberately skips — so this
 * list shows number, customer, dates and status, and the amount lives on
 * the detail screen. A `gross_amount` on the summary is the one ask this
 * screen has of the backend (recorded in the report), and the column is
 * ready for it.
 */
export function InvoiceListScreen() {
  const { t, date } = useI18n();
  const navigate = useNavigate();
  const { administration } = useAdministration();
  const { invoices } = useServices();
  const [rows, setRows] = useState<readonly SalesInvoiceSummaryView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

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

  return (
    <section className="screen" aria-label={t("invoice.list.title")} data-testid="invoice-list">
      <PageHeader
        title={t("invoice.list.title")}
        context={rows === null ? undefined : t("invoice.list.count", { count: rows.length })}
        action={
          <Link to="/invoices/new" className="button-link button-link--primary" data-testid="invoice-list-new">
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
        <div className="panel table-wrap">
          <table className="table" data-testid="invoice-table">
            <thead>
              <tr>
                <th scope="col">{t("invoice.list.column.number")}</th>
                <th scope="col">{t("invoice.list.column.customer")}</th>
                <th scope="col">{t("invoice.list.column.date")}</th>
                <th scope="col">{t("invoice.list.column.due")}</th>
                <th scope="col">{t("invoice.list.column.status")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((invoice) => (
                <tr key={invoice.id} data-testid="invoice-row">
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
                  <td className="ledgr-num">{date(invoice.invoice_date)}</td>
                  <td className="ledgr-num table__muted">
                    {invoice.due_date === null ? "—" : date(invoice.due_date)}
                  </td>
                  <td>
                    <InvoiceStatusChip status={invoice.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function InvoiceStatusChip({ status }: { status: SalesInvoiceStatus }) {
  const { t } = useI18n();
  return (
    <span className={status === "issued" ? "chip chip--positive" : "chip chip--caution"} data-testid="invoice-status">
      {t(`mobile.view.invoice_status.${status}`)}
    </span>
  );
}
