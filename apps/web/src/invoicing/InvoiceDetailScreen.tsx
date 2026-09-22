import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { SalesInvoiceView } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { InvoiceStatusChip } from "./InvoiceListScreen";

/**
 * `/invoices/:id` — one invoice: its lines and VAT breakdown (every figure
 * through `money()`, strings end to end — NFR-031), the rendered PDF
 * where one exists (FR-TPL-017), the send action, and a timeline built
 * from the dates the view actually carries.
 *
 * The PDF is fetched as a Blob and shown through an object URL, never an
 * `<iframe src="/v1/...">` at the download endpoint — SEC-005's attachment
 * disposition, the same technique the purchases screen's `OriginalDocument` also
 * uses.
 *
 * A draft cannot be sent from here: the four-call sequence (lines, issue,
 * send) is `SendInvoiceForm`'s, and the statutory gate runs at issue. A
 * draft's action is therefore "finish it", which is the new-invoice
 * screen. An issued invoice's is "send" (again, if need be — the server
 * decides what a resend means).
 */
export function InvoiceDetailScreen() {
  const { t, money, date } = useI18n();
  const { invoiceId = "" } = useParams();
  const { administration } = useAdministration();
  const { invoices } = useServices();
  const [invoice, setInvoice] = useState<SalesInvoiceView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [sendState, setSendState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [sendProblem, setSendProblem] = useState<string | null>(null);
  // SI-01: "Send" reveals this rather than sending immediately, so there is
  // always a chance to add a note first - composeOpen closes again on a
  // successful send or an explicit cancel, never on its own.
  const [composeOpen, setComposeOpen] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    let cancelled = false;
    setInvoice(null);
    setProblem(null);
    invoices
      .getInvoice(administration.id, invoiceId)
      .then((result) => {
        if (!cancelled) setInvoice(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [invoices, administration.id, invoiceId, attempt]);

  const send = useCallback(async () => {
    setSendState("sending");
    setSendProblem(null);
    try {
      const trimmed = message.trim();
      if (trimmed === "") {
        await invoices.sendInvoice(administration.id, invoiceId);
      } else {
        await invoices.sendInvoice(administration.id, invoiceId, { message: trimmed });
      }
      setSendState("sent");
      setComposeOpen(false);
    } catch (error) {
      setSendProblem(describeError(error));
      setSendState("failed");
    }
  }, [invoices, administration.id, invoiceId, message]);

  if (problem !== null) {
    return (
      <section className="screen" aria-label={t("invoice.detail.title")}>
        <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)}>
          <Link to="/invoices" className="button-link">
            {t("invoice.detail.back")}
          </Link>
        </ErrorState>
      </section>
    );
  }
  if (invoice === null) {
    return (
      <section className="screen" aria-label={t("invoice.detail.title")}>
        <LoadingSkeleton rows={5} />
      </section>
    );
  }

  const title = invoice.invoice_reference ?? t("invoice.list.draft_reference");

  return (
    <section className="screen" aria-label={t("invoice.detail.title")} data-testid="invoice-detail">
      <PageHeader
        title={t("invoice.detail.heading", { reference: title })}
        context={invoice.customer_name}
        action={
          invoice.status === "issued" ? (
            <button
              type="button"
              className="button--primary"
              disabled={sendState === "sending" || composeOpen}
              data-testid="invoice-send"
              onClick={() => setComposeOpen(true)}
            >
              {sendState === "sending" ? t("mobile.invoice.sending") : t("invoice.detail.send")}
            </button>
          ) : (
            <Link
              to="/invoices/new"
              className="button-link button-link--primary"
              data-testid="invoice-finish"
            >
              {t("invoice.detail.finish_draft")}
            </Link>
          )
        }
      >
        <InvoiceStatusChip status={invoice.status} />
      </PageHeader>

      <p className="meta-line">
        <Link to="/invoices">{t("invoice.detail.back")}</Link>
      </p>

      {sendState === "sent" ? (
        <p role="status" className="alert alert--positive" data-testid="invoice-sent">
          {t("mobile.invoice.sent")}
        </p>
      ) : null}
      {sendProblem !== null ? (
        <p role="alert" className="alert alert--attention" data-testid="invoice-send-problem">
          {sendProblem}
        </p>
      ) : null}

      {composeOpen ? (
        <div className="panel panel__body form" data-testid="invoice-send-compose">
          <div className="form__field">
            <label htmlFor="invoice-detail-custom-message">
              {t("mobile.invoice.custom_message")}
            </label>
            <textarea
              id="invoice-detail-custom-message"
              rows={3}
              data-testid="invoice-detail-custom-message"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
            />
          </div>
          <div className="form__actions">
            <button
              type="button"
              className="button--primary"
              disabled={sendState === "sending"}
              data-testid="invoice-send-confirm"
              onClick={() => void send()}
            >
              {sendState === "sending" ? t("mobile.invoice.sending") : t("invoice.detail.send")}
            </button>
            <button
              type="button"
              className="button--quiet"
              data-testid="invoice-send-cancel"
              onClick={() => {
                setComposeOpen(false);
                setMessage("");
              }}
            >
              {t("common.action.cancel")}
            </button>
          </div>
        </div>
      ) : null}

      {invoice.statutory_failures.length > 0 ? (
        <div className="alert alert--caution" data-testid="invoice-failures">
          <ul>
            {invoice.statutory_failures.map((failure, index) => (
              <li key={`${failure.field}-${index}`}>{failure.message}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <dl className="facts panel panel__body">
        <div>
          <dt className="label">{t("mobile.invoice.customer_heading")}</dt>
          <dd>{invoice.customer_name}</dd>
          <dd className="caption">{invoice.customer_address}</dd>
          {invoice.customer_vat_number !== null ? (
            <dd className="caption ledgr-num">{invoice.customer_vat_number}</dd>
          ) : null}
        </div>
        <div>
          <dt className="label">{t("mobile.invoice.invoice_date")}</dt>
          <dd className="ledgr-num">{date(invoice.invoice_date)}</dd>
        </div>
        <div>
          <dt className="label">{t("invoice.list.column.due")}</dt>
          <dd className="ledgr-num">{invoice.due_date === null ? "—" : date(invoice.due_date)}</dd>
        </div>
        <div>
          <dt className="label">{t("invoice.detail.total")}</dt>
          <dd className="figure" data-testid="invoice-gross">
            {money(invoice.gross_amount)}
          </dd>
          <dd className="caption">
            {t("invoice.detail.net_and_vat", {
              net: money(invoice.net_amount),
              vat: money(invoice.vat_amount),
            })}
          </dd>
        </div>
      </dl>

      <section className="screen__section" aria-label={t("mobile.invoice.lines_heading")}>
        <h2>{t("mobile.invoice.lines_heading")}</h2>
        <div className="panel table-wrap">
          <table className="table" data-testid="invoice-lines">
            <thead>
              <tr>
                <th scope="col">{t("mobile.invoice.line_description")}</th>
                <th scope="col" className="table__num">
                  {t("mobile.invoice.line_quantity")}
                </th>
                <th scope="col" className="table__num">
                  {t("mobile.invoice.line_unit_price")}
                </th>
                <th scope="col">{t("mobile.invoice.line_vat_treatment")}</th>
                <th scope="col" className="table__num">
                  {t("invoice.detail.line_net")}
                </th>
              </tr>
            </thead>
            <tbody>
              {invoice.lines.map((line) => (
                <tr key={line.id}>
                  <td>{line.description}</td>
                  <td className="table__num">{line.quantity}</td>
                  <td className="table__num">{money(line.unit_price)}</td>
                  <td>{t(`capture.vat.${line.vat_treatment}`)}</td>
                  <td className="table__num">{money(line.line_net)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              {invoice.vat_groups.map((group) => (
                <tr key={`${group.vat_treatment}-${group.role}`}>
                  <td colSpan={4}>
                    {group.rate === null
                      ? t("invoice.detail.vat_group_no_rate", { treatment: group.vat_treatment })
                      : t("invoice.detail.vat_group", {
                          rate: group.rate,
                          taxable: money(group.taxable_amount),
                        })}
                  </td>
                  <td className="table__num">{money(group.vat_amount)}</td>
                </tr>
              ))}
              <tr>
                <td colSpan={4}>{t("invoice.detail.total")}</td>
                <td className="table__num">{money(invoice.gross_amount)}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </section>

      <section className="screen__section" aria-label={t("invoice.detail.timeline")}>
        <h2>{t("invoice.detail.timeline")}</h2>
        <ol className="timeline panel panel__body" data-testid="invoice-timeline">
          <li>
            <span className="timeline__when">{date(invoice.invoice_date)}</span>
            <span>{t("invoice.detail.event.created")}</span>
          </li>
          {invoice.issued_at !== null ? (
            <li>
              <span className="timeline__when">{date(invoice.issued_at.slice(0, 10))}</span>
              <span>
                {invoice.invoice_number === null
                  ? t("invoice.detail.event.issued")
                  : t("invoice.detail.event.issued_number", { number: invoice.invoice_number })}
              </span>
            </li>
          ) : null}
          {invoice.journal_entry_id !== null ? (
            <li>
              <span className="timeline__when">
                {invoice.issued_at === null ? "—" : date(invoice.issued_at.slice(0, 10))}
              </span>
              <span>{t("invoice.detail.event.posted")}</span>
            </li>
          ) : null}
          {sendState === "sent" ? (
            <li>
              <span className="timeline__when">{t("invoice.detail.event.now")}</span>
              <span>{t("invoice.detail.event.sent")}</span>
            </li>
          ) : null}
        </ol>
      </section>

      <section className="screen__section" aria-label={t("invoice.detail.pdf")}>
        <h2>{t("invoice.detail.pdf")}</h2>
        {invoice.document_id === null ? (
          <p className="caption" data-testid="invoice-pdf-none">
            {t("invoice.detail.pdf_none")}
          </p>
        ) : (
          <PdfPreview administrationId={administration.id} documentId={invoice.document_id} />
        )}
      </section>
    </section>
  );
}

function PdfPreview({
  administrationId,
  documentId,
}: {
  administrationId: string;
  documentId: string;
}) {
  const { t } = useI18n();
  const { invoices } = useServices();
  const [url, setUrl] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    setUrl(null);
    setProblem(null);
    invoices
      .fetchDocumentBlob(administrationId, documentId)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [invoices, administrationId, documentId]);

  if (problem !== null) return <ErrorState message={problem} testId="invoice-pdf-error" />;
  if (url === null) return <LoadingSkeleton rows={1} testId="invoice-pdf-loading" />;
  return (
    <iframe
      className="document-frame"
      title={t("invoice.detail.pdf")}
      src={url}
      data-testid="invoice-pdf-frame"
    />
  );
}
