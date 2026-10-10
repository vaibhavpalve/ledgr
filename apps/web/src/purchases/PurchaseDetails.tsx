import type { ReactNode } from "react";
import { useI18n } from "@ledgr/i18n";
import type { ExpenseView } from "@ledgr/shared-types";

/**
 * Everything the invoice says about itself, beside the original it was read from.
 *
 * What an accountant's package shows next to the scanned PDF: the invoice's own
 * data, the file it came from and a short history. Before this the space beside
 * a submitted invoice held one sentence ("nothing left for you to review") and
 * the person had to read the PDF to learn what was read from it.
 *
 * --- Read-only, and only what is true ---
 *
 * Every row is a fact the server already holds; none is derived here. A row for
 * something not known (a due date the reading did not capture) is left out
 * rather than shown empty, because a row of dashes reads as "we failed to show
 * this" and not as "this was not on the invoice".
 *
 * A draft shows the file and history but NOT the invoice data: those fields are
 * the editable form beside it, and a read-only copy of a field somebody is
 * typing into would be stale by the next keystroke.
 *
 * --- Dates, not times ---
 *
 * The shared formatter takes a calendar date and refuses an instant
 * (FR-LOC-002 renders in the administration's locale, and the catalogue has no
 * time format), so each timestamp is shown as the day it happened.
 */
export function PurchaseDetails({ expense }: { expense: ExpenseView }) {
  const submitted = expense.status !== "draft";

  return (
    <div className="purchase-details" data-testid="purchase-details">
      {submitted ? <InvoiceData expense={expense} /> : null}
      <FileCard expense={expense} />
      <History expense={expense} />
    </div>
  );
}

function Card({
  title,
  testId,
  children,
}: {
  title: string;
  testId: string;
  children: ReactNode;
}) {
  return (
    <section className="purchase-card" aria-label={title} data-testid={testId}>
      <h2 className="purchase-card__title">{title}</h2>
      {children}
    </section>
  );
}

function Row({
  label,
  children,
  testId,
}: {
  label: string;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <div className="purchase-card__row">
      <dt>{label}</dt>
      <dd {...(testId !== undefined ? { "data-testid": testId } : {})}>{children}</dd>
    </div>
  );
}

function InvoiceData({ expense }: { expense: ExpenseView }) {
  const { t, money, date } = useI18n();
  const none = t("capture.details.none");

  return (
    <Card title={t("capture.details.invoice_title")} testId="purchase-invoice-data">
      <dl className="purchase-card__rows">
        <Row label={t("capture.form.supplier")} testId="detail-supplier">
          {expense.supplier ?? none}
        </Row>
        <Row label={t("capture.form.invoice_number")} testId="detail-invoice-number">
          {expense.invoice_number ?? none}
        </Row>
        <Row label={t("capture.form.date")} testId="detail-date">
          {expense.expense_date !== null ? date(expense.expense_date) : none}
        </Row>
        <Row label={t("capture.details.total")} testId="detail-total">
          <strong>{expense.gross_amount !== null ? money(expense.gross_amount) : none}</strong>
        </Row>
        <Row label={t("capture.details.vat")} testId="detail-vat">
          {expense.vat_treatment !== null ? t(`capture.vat.${expense.vat_treatment}`) : none}
          {expense.vat_amount !== null ? ` · ${money(expense.vat_amount)}` : ""}
        </Row>
        <Row label={t("capture.details.net")} testId="detail-net">
          {expense.net_amount !== null ? money(expense.net_amount) : none}
        </Row>
        <Row label={t("capture.form.category")} testId="detail-category">
          {expense.category ?? none}
        </Row>
        <Row label={t("capture.details.payment")} testId="detail-payment">
          {expense.payment_method !== null ? t(`capture.payment.${expense.payment_method}`) : none}
        </Row>
        <Row label={t("capture.purchases.col.status")} testId="detail-status">
          <span className={`purchases__status purchases__status--${expense.status}`}>
            {t(`capture.purchases.status.${expense.status}`)}
          </span>
        </Row>
      </dl>
    </Card>
  );
}

function FileCard({ expense }: { expense: ExpenseView }) {
  const { t, date, number } = useI18n();
  const none = t("capture.details.none");
  const original = expense.original ?? null;
  const reading = expense.extraction ?? null;

  const readingKey =
    reading === null
      ? "capture.details.reading.none"
      : `capture.details.reading.${reading.status}`;

  return (
    <Card title={t("capture.details.file_title")} testId="purchase-file">
      <dl className="purchase-card__rows">
        {original !== null ? (
          <>
            <Row label={t("capture.details.file_name")} testId="detail-file-name">
              <span className="purchase-card__wrap">{original.filename ?? none}</span>
            </Row>
            <Row label={t("capture.details.file_type")} testId="detail-file-type">
              {typeLabel(original.content_type)}
              {` · ${size(original.byte_size, number)}`}
              {original.page_count > 1
                ? ` · ${t("capture.details.pages", { count: original.page_count })}`
                : ""}
            </Row>
            <Row label={t("capture.details.source")} testId="detail-source">
              {t(`capture.details.source.${original.source}`)}
            </Row>
          </>
        ) : null}
        {expense.created_at ? (
          <Row label={t("capture.details.uploaded")} testId="detail-uploaded">
            {date(expense.created_at.slice(0, 10), "long")}
          </Row>
        ) : null}
        <Row label={t("capture.details.reading")} testId="detail-reading">
          {t(readingKey)}
        </Row>
      </dl>
    </Card>
  );
}

/** One line per thing that happened, newest last, each with the day it did when known. */
function History({ expense }: { expense: ExpenseView }) {
  const { t, date } = useI18n();
  const original = expense.original ?? null;
  const reading = expense.extraction ?? null;
  const day = (instant: string | null | undefined) =>
    instant ? date(instant.slice(0, 10), "long") : null;

  const entries: { key: string; label: string; when: string | null }[] = [];

  if (expense.created_at) {
    entries.push({
      key: "captured",
      label: t(`capture.details.history.captured.${original?.source ?? "upload"}`),
      when: day(expense.created_at),
    });
  }
  if (reading !== null && reading.status === "done") {
    entries.push({
      key: "read",
      label: t("capture.details.history.read"),
      when: day(reading.read_at),
    });
  } else if (reading !== null && reading.status === "failed") {
    entries.push({
      key: "read_failed",
      label: t("capture.details.history.read_failed"),
      when: day(reading.read_at),
    });
  }
  if (expense.status !== "draft") {
    // Submitted without a person when the reading was certain of the rate; a
    // person's own submit keeps no time, so that entry has no date.
    entries.push(
      reading?.submitted === true
        ? {
            key: "submitted",
            label: t("capture.details.history.submitted_auto"),
            when: day(reading.read_at),
          }
        : { key: "submitted", label: t("capture.details.history.submitted"), when: null },
    );
  }
  if (expense.status === "posted") {
    entries.push({
      key: "posted",
      label: t("capture.details.history.posted"),
      when: day(expense.posted_at),
    });
  }

  if (entries.length === 0) return null;

  return (
    <Card title={t("capture.details.history_title")} testId="purchase-history">
      <ol className="purchase-history">
        {entries.map((entry) => (
          <li key={entry.key} data-testid={`history-${entry.key}`}>
            <span className="purchase-history__label">{entry.label}</span>
            {entry.when !== null ? (
              <span className="purchase-history__when">{entry.when}</span>
            ) : null}
          </li>
        ))}
      </ol>
    </Card>
  );
}

/** "application/pdf" -> "PDF", "image/jpeg" -> "JPEG": what a person calls it. */
function typeLabel(contentType: string): string {
  const subtype = contentType.split("/")[1] ?? contentType;
  return subtype.toUpperCase();
}

/** Bytes as KB or MB, rounded to one decimal, from the decimal text `number()` takes. */
function size(bytes: number, number: ReturnType<typeof useI18n>["number"]): string {
  if (bytes < 1024 * 1024) {
    return `${number((bytes / 1024).toFixed(1), { scale: 1 })} KB`;
  }
  return `${number((bytes / (1024 * 1024)).toFixed(1), { scale: 1 })} MB`;
}
