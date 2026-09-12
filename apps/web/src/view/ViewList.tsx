import { useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type {
  ExpenseSummaryView,
  ExpenseStatus,
  SalesInvoiceStatus,
  SalesInvoiceSummaryView,
} from "@ledgr/shared-types";

import type { CaptureApi } from "../capture/api";
import type { SalesInvoiceApi } from "../invoicing/api";

/**
 * MOB-004's View tab: a read-only look at what has already left capture —
 * posted/ready expenses and issued (plus draft, so a half-finished
 * mobile-created invoice is resumable) sales invoices — each tappable to a
 * minimal detail with, where one exists, the document at full resolution.
 *
 * Two independent lists rather than one merged feed: an expense and an
 * invoice are different documents with different detail views, and merging
 * them into one list would need a synthetic sort key neither the expense nor
 * the invoicing backend defines.
 */
export function ViewList({
  administrationId,
  captureApi,
  invoiceApi,
}: {
  administrationId: string;
  captureApi: CaptureApi;
  invoiceApi: SalesInvoiceApi;
}) {
  const { t, money, date } = useI18n();
  const [expenses, setExpenses] = useState<readonly ExpenseSummaryView[] | null>(null);
  const [invoices, setInvoices] = useState<readonly SalesInvoiceSummaryView[] | null>(null);
  const [problem, setProblem] = useState(false);
  const [selected, setSelected] = useState<Selection | null>(null);

  useEffect(() => {
    let cancelled = false;
    setProblem(false);
    Promise.all([
      captureApi.listExpenses(administrationId),
      invoiceApi.listInvoices(administrationId),
    ])
      .then(([expenseRows, invoiceRows]) => {
        if (cancelled) return;
        // Drafts belong to the Approve tab, not here — MOB-004's View tab is
        // "posted/ready expenses". Filtered client-side rather than with a
        // second server round trip per status: one list call already has
        // every status, and there are at most two to keep.
        setExpenses(expenseRows.filter((expense) => expense.status !== "draft"));
        setInvoices(invoiceRows);
      })
      .catch(() => {
        if (!cancelled) setProblem(true);
      });
    return () => {
      cancelled = true;
    };
  }, [administrationId, captureApi, invoiceApi]);

  if (selected !== null) {
    return (
      <section aria-label={t("mobile.view.title")}>
        <button type="button" data-testid="view-back" onClick={() => setSelected(null)}>
          {t("mobile.view.back")}
        </button>
        {selected.documentId !== null ? (
          <DocumentViewer
            administrationId={administrationId}
            documentId={selected.documentId}
            api={invoiceApi}
          />
        ) : (
          <p data-testid="view-document-none">{t("mobile.view.document_none")}</p>
        )}
      </section>
    );
  }

  return (
    <section aria-label={t("mobile.view.title")}>
      <h1>{t("mobile.view.title")}</h1>

      {problem ? (
        <p role="alert" data-testid="view-error">
          {t("mobile.common.error")}
        </p>
      ) : null}

      <section aria-label={t("mobile.view.expenses_heading")}>
        <h2>{t("mobile.view.expenses_heading")}</h2>
        {expenses === null && !problem ? (
          <p role="status" data-testid="view-expenses-loading">
            {t("mobile.common.loading")}
          </p>
        ) : null}
        {expenses !== null && expenses.length === 0 ? (
          <p data-testid="view-expenses-empty">{t("mobile.view.empty_expenses")}</p>
        ) : null}
        {expenses !== null && expenses.length > 0 ? (
          <ul data-testid="view-expenses-list">
            {expenses.map((expense) => (
              <li key={expense.id} data-testid="view-expense-item">
                <button
                  type="button"
                  data-testid={`view-expense-${expense.id}`}
                  onClick={() => setSelected({ kind: "expense", documentId: null })}
                >
                  <span>{expense.supplier ?? t("mobile.approve.untitled")}</span>
                  {expense.expense_date !== null ? <span>{date(expense.expense_date)}</span> : null}
                  {expense.gross_amount !== null ? (
                    <span>{money(expense.gross_amount)}</span>
                  ) : null}
                  <span>{t(expenseStatusKey(expense.status))}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section aria-label={t("mobile.view.invoices_heading")}>
        <h2>{t("mobile.view.invoices_heading")}</h2>
        {invoices === null && !problem ? (
          <p role="status" data-testid="view-invoices-loading">
            {t("mobile.common.loading")}
          </p>
        ) : null}
        {invoices !== null && invoices.length === 0 ? (
          <p data-testid="view-invoices-empty">{t("mobile.view.empty_invoices")}</p>
        ) : null}
        {invoices !== null && invoices.length > 0 ? (
          <ul data-testid="view-invoices-list">
            {invoices.map((invoice) => (
              <li key={invoice.id} data-testid="view-invoice-item">
                <button
                  type="button"
                  data-testid={`view-invoice-${invoice.id}`}
                  onClick={() => setSelected({ kind: "invoice", documentId: invoice.document_id })}
                >
                  <span>{invoice.customer_name}</span>
                  <span>{date(invoice.invoice_date)}</span>
                  {invoice.invoice_reference !== null ? (
                    <span>{invoice.invoice_reference}</span>
                  ) : null}
                  <span>{t(invoiceStatusKey(invoice.status))}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </section>
    </section>
  );
}

interface Selection {
  readonly kind: "expense" | "invoice";
  /**
   * MOB-004's "the document viewable at full resolution". Sales invoices
   * carry `document_id` on their own summary row (the rendered PDF, FR-TPL-017).
   * Expenses do not yet — no endpoint links an expense to its capture pages'
   * document ids today (see this feature's ADR, Known gaps) — so this is
   * always null for an expense selection.
   */
  readonly documentId: string | null;
}

function expenseStatusKey(status: ExpenseStatus): string {
  return `mobile.view.expense_status.${status}`;
}

function invoiceStatusKey(status: SalesInvoiceStatus): string {
  return `mobile.view.invoice_status.${status}`;
}

/**
 * MOB-004's "document viewable at full resolution" — `fetch()` + `Blob` +
 * `URL.createObjectURL`, the exact technique `TemplateDesigner`'s logo
 * preview and `TemplateApi.fetchTemplateAssetBlob` already establish, never a
 * direct `<img src="/v1/...">`/`<iframe src="/v1/...">` at the download
 * endpoint (SEC-005 — that endpoint sends `Content-Disposition: attachment`
 * precisely so nothing navigates to it directly).
 */
function DocumentViewer({
  administrationId,
  documentId,
  api,
}: {
  administrationId: string;
  documentId: string;
  api: SalesInvoiceApi;
}) {
  const { t } = useI18n();
  const [url, setUrl] = useState<string | null>(null);
  const [problem, setProblem] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    setUrl(null);
    setProblem(false);
    api
      .fetchDocumentBlob(administrationId, documentId)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setProblem(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [administrationId, api, documentId]);

  return (
    <section aria-label={t("mobile.view.document_heading")}>
      <h2>{t("mobile.view.document_heading")}</h2>
      {problem ? (
        <p role="alert" data-testid="view-document-error">
          {t("mobile.common.error")}
        </p>
      ) : null}
      {!problem && url === null ? (
        <p role="status" data-testid="view-document-loading">
          {t("mobile.view.document_loading")}
        </p>
      ) : null}
      {url !== null ? (
        <iframe
          title={t("mobile.view.document_heading")}
          data-testid="view-document-frame"
          src={url}
        />
      ) : null}
    </section>
  );
}
