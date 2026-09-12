import { useCallback, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import { VAT_TREATMENTS } from "@ledgr/shared-types";
import type { StatutoryFailureView, VatTreatment } from "@ledgr/shared-types";

import {
  ApiError,
  InvoiceCustomerProblemError,
  InvoiceNotCompliantError,
  type CreateInvoiceBody,
  type InvoiceLineBody,
  type SalesInvoiceApi,
} from "./api";

/**
 * MOB-005's Send-invoice tab: "the phone renders and sends" using the
 * administration's template — CREATION and SENDING, never template editing
 * (that stays web-only in `TemplateDesigner.tsx`, untouched by this form).
 *
 * --- Typed customer entry, not a picker (this task's documented MVP choice) ---
 *
 * `CustomerDetailsMissing`'s contract is `customer_id` XOR typed fields; this
 * form always sends the typed path. A customer-picker is a nice-to-have
 * deferred to a later pass (see this feature's ADR) — typed entry alone
 * satisfies MOB-005 and is the simpler, correct P0 shape.
 *
 * --- The four-call sequence, sequenced here rather than in the client ---
 *
 *     create (draft) -> set lines -> issue -> send
 *
 * `SalesInvoiceApi` exposes each as its own method, mirroring
 * `api.invoicing.routes`'s own four endpoints; this component is what walks
 * them in order, exactly as `issue`'s own docstring explains why the two
 * things that can fail (the statutory gate, the posting configuration) must
 * both run before the one thing that cannot be undone (the number). A
 * created draft's id is remembered across a retry so that resubmitting after
 * a failed later step edits the SAME draft rather than creating a second one.
 *
 * --- Server-validated, shown at its own control ---
 *
 * `InvoiceCustomerProblemError` (from `create`) and `InvoiceNotCompliantError`
 * (from `issue`, FR-AR-003's statutory gate — the same `statutory_failures`
 * shape `GET .../sales-invoices/{id}` already returns) are placed at the
 * field or line the server named, never a generic banner — the same
 * discipline `TemplateDesigner`'s violations and `ExpenseForm`'s
 * `duplicate_warnings` already follow. A failure this form has no specific
 * control for still has to be shown (the server is the authority, CLAUDE.md
 * rule 3) — it falls into an "other problems" list, mirroring
 * `TemplateDesigner`'s own fallback.
 */
export function SendInvoiceForm({
  administrationId,
  fiscalYearId,
  api,
  onSent,
}: {
  administrationId: string;
  fiscalYearId: string;
  api: SalesInvoiceApi;
  /**
   * Called when, after a successful send, the person asks to go to the View
   * tab — see the "sent" screen's own button below.
   */
  onSent?: () => void;
}) {
  const { t } = useI18n();

  const [invoiceId, setInvoiceId] = useState<string | null>(null);
  const [customerName, setCustomerName] = useState("");
  const [customerAddress, setCustomerAddress] = useState("");
  const [customerCountry, setCustomerCountry] = useState("NL");
  const [customerVatNumber, setCustomerVatNumber] = useState("");
  const [invoiceDate, setInvoiceDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [lines, setLines] = useState<DraftLine[]>([emptyLine()]);

  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [customerProblem, setCustomerProblem] = useState<{
    message: string;
    fields: readonly string[];
  } | null>(null);
  const [failures, setFailures] = useState<readonly StatutoryFailureView[]>([]);

  const submit = useCallback(async () => {
    setSending(true);
    setProblem(null);
    setCustomerProblem(null);
    setFailures([]);
    try {
      let id = invoiceId;
      if (id === null) {
        const body: CreateInvoiceBody = {
          fiscal_year_id: fiscalYearId,
          invoice_date: invoiceDate,
          customer_name: customerName,
          customer_address: customerAddress,
          customer_country: customerCountry,
          customer_vat_number: customerVatNumber || null,
        };
        const created = await api.createInvoice(administrationId, body);
        id = created.id;
        setInvoiceId(id);
      }

      const lineBodies: InvoiceLineBody[] = lines.map((line) => ({
        description: line.description,
        quantity: line.quantity,
        unit_price: line.unitPrice,
        vat_treatment: line.vatTreatment,
        discount_percent: line.discountPercent || "0",
      }));
      await api.setInvoiceLines(administrationId, id, lineBodies);
      await api.issueInvoice(administrationId, id);
      await api.sendInvoice(administrationId, id);

      setSent(true);
    } catch (error) {
      if (error instanceof InvoiceCustomerProblemError) {
        setCustomerProblem({ message: error.message, fields: error.fields });
      } else if (error instanceof InvoiceNotCompliantError) {
        setFailures(error.failures);
      } else if (error instanceof ApiError) {
        setProblem(error.message);
      } else {
        setProblem(error instanceof Error ? error.message : "");
      }
    } finally {
      setSending(false);
    }
  }, [
    administrationId,
    api,
    customerAddress,
    customerCountry,
    customerName,
    customerVatNumber,
    fiscalYearId,
    invoiceDate,
    invoiceId,
    lines,
  ]);

  if (sent) {
    return (
      <section aria-label={t("mobile.invoice.title")}>
        <p role="status" data-testid="invoice-sent">
          {t("mobile.invoice.sent")}
        </p>
        {/*
          "Show a clear confirmation and return to (or offer to return to)
          the View tab" (MOB-005) - offered rather than automatic, so the
          confirmation itself stays on screen until the person is done
          reading it rather than being replaced by a tab switch mid-sentence.
        */}
        <button type="button" data-testid="invoice-back-to-view" onClick={() => onSent?.()}>
          {t("mobile.invoice.back_to_view")}
        </button>
      </section>
    );
  }

  const unmapped = failures.filter((failure) => !isMappedField(failure.field));

  return (
    <form
      className="send-invoice-form"
      aria-label={t("mobile.invoice.title")}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h1>{t("mobile.invoice.title")}</h1>

      <section aria-label={t("mobile.invoice.customer_heading")}>
        <h2>{t("mobile.invoice.customer_heading")}</h2>

        {customerProblem !== null ? (
          <p role="alert" data-testid="invoice-customer-problem">
            {customerProblem.message}
          </p>
        ) : null}

        <label>
          {t("mobile.invoice.customer_name")}
          <input
            type="text"
            data-testid="invoice-customer-name"
            value={customerName}
            onChange={(event) => setCustomerName(event.target.value)}
          />
        </label>
        <FieldProblem
          failure={findFailure(failures, "customer_name")}
          testId="invoice-field-customer_name"
        />

        <label>
          {t("mobile.invoice.customer_address")}
          <input
            type="text"
            data-testid="invoice-customer-address"
            value={customerAddress}
            onChange={(event) => setCustomerAddress(event.target.value)}
          />
        </label>
        <FieldProblem
          failure={findFailure(failures, "customer_address")}
          testId="invoice-field-customer_address"
        />

        <label>
          {t("mobile.invoice.customer_country")}
          <input
            type="text"
            data-testid="invoice-customer-country"
            value={customerCountry}
            onChange={(event) => setCustomerCountry(event.target.value)}
          />
        </label>

        <label>
          {t("mobile.invoice.customer_vat_number")}
          <input
            type="text"
            data-testid="invoice-customer-vat-number"
            value={customerVatNumber}
            onChange={(event) => setCustomerVatNumber(event.target.value)}
          />
        </label>
        <FieldProblem
          failure={findFailure(failures, "customer_vat_number")}
          testId="invoice-field-customer_vat_number"
        />
      </section>

      <label>
        {t("mobile.invoice.invoice_date")}
        <input
          type="date"
          data-testid="invoice-date"
          value={invoiceDate}
          onChange={(event) => setInvoiceDate(event.target.value)}
        />
      </label>
      <FieldProblem
        failure={findFailure(failures, "invoice_date")}
        testId="invoice-field-invoice_date"
      />

      <section aria-label={t("mobile.invoice.lines_heading")}>
        <h2>{t("mobile.invoice.lines_heading")}</h2>
        <FieldProblem failure={findFailure(failures, "lines")} testId="invoice-field-lines" />

        {lines.map((line, index) => {
          const position = index + 1;
          return (
            <div key={line.key} data-testid={`invoice-line-${position}`}>
              <label>
                {t("mobile.invoice.line_description")}
                <input
                  type="text"
                  data-testid={`invoice-line-${position}-description`}
                  value={line.description}
                  onChange={(event) =>
                    setLines((current) =>
                      updateLine(current, index, { description: event.target.value }),
                    )
                  }
                />
              </label>
              <FieldProblem
                failure={findFailure(failures, "line_description", position)}
                testId={`invoice-line-${position}-description-problem`}
              />

              <label>
                {t("mobile.invoice.line_quantity")}
                <input
                  type="text"
                  inputMode="decimal"
                  data-testid={`invoice-line-${position}-quantity`}
                  value={line.quantity}
                  onChange={(event) =>
                    setLines((current) =>
                      updateLine(current, index, { quantity: event.target.value }),
                    )
                  }
                />
              </label>
              <FieldProblem
                failure={findFailure(failures, "line_quantity", position)}
                testId={`invoice-line-${position}-quantity-problem`}
              />

              <label>
                {t("mobile.invoice.line_unit_price")}
                <input
                  type="text"
                  inputMode="decimal"
                  data-testid={`invoice-line-${position}-unit-price`}
                  value={line.unitPrice}
                  onChange={(event) =>
                    setLines((current) =>
                      updateLine(current, index, { unitPrice: event.target.value }),
                    )
                  }
                />
              </label>
              <FieldProblem
                failure={findFailure(failures, "line_unit_price", position)}
                testId={`invoice-line-${position}-unit-price-problem`}
              />

              <label>
                {t("mobile.invoice.line_discount")}
                <input
                  type="text"
                  inputMode="decimal"
                  data-testid={`invoice-line-${position}-discount`}
                  value={line.discountPercent}
                  onChange={(event) =>
                    setLines((current) =>
                      updateLine(current, index, { discountPercent: event.target.value }),
                    )
                  }
                />
              </label>

              <label>
                {t("mobile.invoice.line_vat_treatment")}
                <select
                  data-testid={`invoice-line-${position}-vat-treatment`}
                  value={line.vatTreatment}
                  onChange={(event) =>
                    setLines((current) =>
                      updateLine(current, index, {
                        vatTreatment: event.target.value as VatTreatment,
                      }),
                    )
                  }
                >
                  {VAT_TREATMENTS.map((treatment) => (
                    <option key={treatment} value={treatment}>
                      {t(`capture.vat.${treatment}`)}
                    </option>
                  ))}
                </select>
              </label>
              <FieldProblem
                failure={findFailure(failures, "vat_treatment", position)}
                testId={`invoice-line-${position}-vat-treatment-problem`}
              />

              {lines.length > 1 ? (
                <button
                  type="button"
                  data-testid={`invoice-line-${position}-remove`}
                  onClick={() => setLines((current) => current.filter((_, i) => i !== index))}
                >
                  {t("mobile.invoice.remove_line")}
                </button>
              ) : null}
            </div>
          );
        })}

        <button
          type="button"
          data-testid="invoice-add-line"
          onClick={() => setLines((current) => [...current, emptyLine()])}
        >
          {t("mobile.invoice.add_line")}
        </button>
      </section>

      {unmapped.length > 0 ? (
        <section
          aria-label={t("mobile.invoice.other_problems")}
          data-testid="invoice-other-problems"
        >
          <h2>{t("mobile.invoice.other_problems")}</h2>
          <ul>
            {unmapped.map((failure, index) => (
              <li key={`${failure.field}-${index}`} role="alert">
                {failure.message}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {problem !== null ? (
        <p role="alert" data-testid="invoice-problem">
          {problem}
        </p>
      ) : null}

      <button type="submit" disabled={sending} data-testid="invoice-submit">
        {sending ? t("mobile.invoice.sending") : t("mobile.invoice.submit")}
      </button>
    </form>
  );
}

interface DraftLine {
  readonly key: string;
  description: string;
  quantity: string;
  unitPrice: string;
  discountPercent: string;
  vatTreatment: VatTreatment;
}

function emptyLine(): DraftLine {
  return {
    key: crypto.randomUUID(),
    description: "",
    quantity: "1",
    unitPrice: "",
    discountPercent: "0",
    vatTreatment: "btw_21",
  };
}

function updateLine(
  current: readonly DraftLine[],
  index: number,
  patch: Partial<DraftLine>,
): DraftLine[] {
  return current.map((line, i) => (i === index ? { ...line, ...patch } : line));
}

/** `field`s this form places at a specific control — everything else falls into "other problems". */
const MAPPED_FIELDS = new Set([
  "customer_name",
  "customer_address",
  "customer_vat_number",
  "invoice_date",
  "lines",
  "line_description",
  "line_quantity",
  "line_unit_price",
  "vat_treatment",
  "vat_rate",
]);

function isMappedField(field: string): boolean {
  return MAPPED_FIELDS.has(field);
}

function findFailure(
  failures: readonly StatutoryFailureView[],
  field: string,
  linePosition: number | null = null,
): StatutoryFailureView | undefined {
  return failures.find(
    (failure) => failure.field === field && failure.line_position === linePosition,
  );
}

function FieldProblem({
  failure,
  testId,
}: {
  failure: StatutoryFailureView | undefined;
  testId: string;
}) {
  if (failure === undefined) return null;
  return (
    <p role="alert" data-testid={testId}>
      {failure.message}
    </p>
  );
}
