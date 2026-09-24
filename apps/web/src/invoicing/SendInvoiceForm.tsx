import { useCallback, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import { VAT_TREATMENTS } from "@ledgr/shared-types";
import type { CustomerView, StatutoryFailureView, VatTreatment } from "@ledgr/shared-types";

import {
  ApiError,
  InvoiceCustomerProblemError,
  InvoiceNotCompliantError,
  type CreateInvoiceBody,
  type InvoiceLineBody,
  type SalesInvoiceApi,
} from "./api";
import { toDecimalInput } from "../ui/decimal";

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
  customers,
  onCreateCustomer,
  onOpenInvoice,
}: {
  administrationId: string;
  fiscalYearId: string;
  api: SalesInvoiceApi;
  /**
   * Called when, after a successful send, the person asks to go to the View
   * tab — see the "sent" screen's own button below.
   */
  onSent?: () => void;
  /**
   * FR-AR-006: the customer master, when the caller has one. Given, the form
   * offers a picker whose choice is sent as `customer_id` — and NO typed
   * fields, because `InvoiceBody` is one or the other. Left out (the mobile
   * shell, this form's own older tests), the typed path is the only one,
   * exactly as ADR-046 §4 shipped it.
   */
  customers?: readonly CustomerView[];
  /** The create-inline path: a new customer, then back here. */
  onCreateCustomer?: () => void;
  /** After a send: open the invoice's own screen (its PDF, its timeline). */
  onOpenInvoice?: (invoiceId: string) => void;
}) {
  const { t, language } = useI18n();

  const [invoiceId, setInvoiceId] = useState<string | null>(null);
  // "" = type the details; otherwise a customer's id from the master.
  const [customerId, setCustomerId] = useState("");
  const [customerName, setCustomerName] = useState("");
  const [customerAddress, setCustomerAddress] = useState("");
  const [customerCountry, setCustomerCountry] = useState("NL");
  const [customerVatNumber, setCustomerVatNumber] = useState("");
  const [invoiceDate, setInvoiceDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [lines, setLines] = useState<DraftLine[]>([emptyLine()]);
  // SI-01. Never sent to `create`/`issue` - only the final `send` call, and
  // only if non-blank (an all-whitespace note is treated as none, both here
  // and server-side).
  const [customMessage, setCustomMessage] = useState("");

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
        const body: CreateInvoiceBody =
          customerId !== ""
            ? { fiscal_year_id: fiscalYearId, invoice_date: invoiceDate, customer_id: customerId }
            : {
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
        // Typed as a person writes it ("95,00"), sent as the API reads it.
        quantity: toDecimalInput(line.quantity, language),
        unit_price: toDecimalInput(line.unitPrice, language),
        vat_treatment: line.vatTreatment,
        discount_percent: toDecimalInput(line.discountPercent, language) || "0",
      }));
      await api.setInvoiceLines(administrationId, id, lineBodies);
      await api.issueInvoice(administrationId, id);
      const trimmedMessage = customMessage.trim();
      // No third argument at all when there is no message - not an explicit
      // `undefined` - so a plain send's call shape is unchanged.
      if (trimmedMessage === "") {
        await api.sendInvoice(administrationId, id);
      } else {
        await api.sendInvoice(administrationId, id, { message: trimmedMessage });
      }

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
    customerId,
    customMessage,
    customerName,
    customerVatNumber,
    fiscalYearId,
    invoiceDate,
    invoiceId,
    language,
    lines,
  ]);

  if (sent) {
    return (
      <section aria-label={t("mobile.invoice.title")} className="screen">
        <div className="panel empty-state">
          <p role="status" className="empty-state__title" data-testid="invoice-sent">
            {t("mobile.invoice.sent")}
          </p>
          <div className="form__actions">
            {/*
              "Show a clear confirmation and return to (or offer to return to)
              the View tab" (MOB-005) - offered rather than automatic, so the
              confirmation itself stays on screen until the person is done
              reading it rather than being replaced by a tab switch mid-sentence.
            */}
            {onOpenInvoice !== undefined && invoiceId !== null ? (
              <button
                type="button"
                className="button--primary"
                data-testid="invoice-open-sent"
                onClick={() => onOpenInvoice(invoiceId)}
              >
                {t("invoice.new.open_sent")}
              </button>
            ) : null}
            <button type="button" data-testid="invoice-back-to-view" onClick={() => onSent?.()}>
              {t("mobile.invoice.back_to_view")}
            </button>
          </div>
        </div>
      </section>
    );
  }

  const unmapped = failures.filter((failure) => !isMappedField(failure.field));
  const pickedCustomer = customers?.find((customer) => customer.id === customerId);

  return (
    <form
      className="send-invoice-form form"
      aria-label={t("mobile.invoice.title")}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h1>{t("mobile.invoice.title")}</h1>

      <section aria-label={t("mobile.invoice.customer_heading")} className="panel panel__body form">
        <h2>{t("mobile.invoice.customer_heading")}</h2>

        {customerProblem !== null ? (
          <p role="alert" className="alert alert--attention" data-testid="invoice-customer-problem">
            {customerProblem.message}
          </p>
        ) : null}

        {customers !== undefined ? (
          <div className="form__field">
            <label htmlFor="invoice-customer-pick">{t("invoice.new.customer_pick")}</label>
            <select
              id="invoice-customer-pick"
              data-testid="invoice-customer-pick"
              value={customerId}
              onChange={(event) => setCustomerId(event.target.value)}
            >
              <option value="">{t("invoice.new.customer_typed")}</option>
              {customers
                .filter((customer) => customer.archived_at === null && customer.erased_at === null)
                .map((customer) => (
                  <option key={customer.id} value={customer.id}>
                    {customer.trade_name ?? customer.name}
                  </option>
                ))}
            </select>
            {onCreateCustomer !== undefined ? (
              <button
                type="button"
                className="button--quiet"
                data-testid="invoice-create-customer"
                onClick={onCreateCustomer}
              >
                {t("invoice.new.customer_create")}
              </button>
            ) : null}
          </div>
        ) : null}

        {pickedCustomer !== undefined ? (
          <dl className="facts" data-testid="invoice-customer-picked">
            <div>
              <dt className="label">{t("mobile.invoice.customer_name")}</dt>
              <dd>{pickedCustomer.name}</dd>
            </div>
            <div>
              <dt className="label">{t("mobile.invoice.customer_address")}</dt>
              <dd>
                {[pickedCustomer.address_line1, pickedCustomer.postal_code, pickedCustomer.city]
                  .filter((part) => part !== null && part !== "")
                  .join(", ")}
              </dd>
              {!pickedCustomer.address_is_complete ? (
                <dd className="field-error">{t("invoice.new.customer_address_incomplete")}</dd>
              ) : null}
            </div>
          </dl>
        ) : (
          <div className="form__grid">
            <div className="form__field">
              <label htmlFor="invoice-customer-name">{t("mobile.invoice.customer_name")}</label>
              <input
                id="invoice-customer-name"
                type="text"
                data-testid="invoice-customer-name"
                value={customerName}
                onChange={(event) => setCustomerName(event.target.value)}
              />
              <FieldProblem
                failure={findFailure(failures, "customer_name")}
                testId="invoice-field-customer_name"
              />
            </div>

            <div className="form__field">
              <label htmlFor="invoice-customer-address">
                {t("mobile.invoice.customer_address")}
              </label>
              <input
                id="invoice-customer-address"
                type="text"
                data-testid="invoice-customer-address"
                value={customerAddress}
                onChange={(event) => setCustomerAddress(event.target.value)}
              />
              <FieldProblem
                failure={findFailure(failures, "customer_address")}
                testId="invoice-field-customer_address"
              />
            </div>

            <div className="form__field">
              <label htmlFor="invoice-customer-country">
                {t("mobile.invoice.customer_country")}
              </label>
              <input
                id="invoice-customer-country"
                type="text"
                data-testid="invoice-customer-country"
                value={customerCountry}
                onChange={(event) => setCustomerCountry(event.target.value)}
              />
            </div>

            <div className="form__field">
              <label htmlFor="invoice-customer-vat">
                {t("mobile.invoice.customer_vat_number")}
              </label>
              <input
                id="invoice-customer-vat"
                type="text"
                data-testid="invoice-customer-vat-number"
                value={customerVatNumber}
                onChange={(event) => setCustomerVatNumber(event.target.value)}
              />
              <FieldProblem
                failure={findFailure(failures, "customer_vat_number")}
                testId="invoice-field-customer_vat_number"
              />
            </div>
          </div>
        )}
      </section>

      <div className="form__field">
        <label htmlFor="invoice-date">{t("mobile.invoice.invoice_date")}</label>
        <input
          id="invoice-date"
          type="date"
          data-testid="invoice-date"
          value={invoiceDate}
          onChange={(event) => setInvoiceDate(event.target.value)}
        />
        <FieldProblem
          failure={findFailure(failures, "invoice_date")}
          testId="invoice-field-invoice_date"
        />
      </div>

      <section aria-label={t("mobile.invoice.lines_heading")} className="panel panel__body form">
        <h2>{t("mobile.invoice.lines_heading")}</h2>
        <FieldProblem failure={findFailure(failures, "lines")} testId="invoice-field-lines" />

        {lines.map((line, index) => {
          const position = index + 1;
          return (
            <div
              key={line.key}
              className="form__grid invoice-line"
              data-testid={`invoice-line-${position}`}
            >
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
                  className="button--quiet form__span"
                  data-testid={`invoice-line-${position}-remove`}
                  onClick={() => setLines((current) => current.filter((_, i) => i !== index))}
                >
                  {t("mobile.invoice.remove_line")}
                </button>
              ) : null}
            </div>
          );
        })}

        <div className="form__actions">
          <button
            type="button"
            data-testid="invoice-add-line"
            onClick={() => setLines((current) => [...current, emptyLine()])}
          >
            {t("mobile.invoice.add_line")}
          </button>
        </div>
      </section>

      {unmapped.length > 0 ? (
        <section
          aria-label={t("mobile.invoice.other_problems")}
          className="alert alert--attention"
          data-testid="invoice-other-problems"
        >
          <div>
            <h2>{t("mobile.invoice.other_problems")}</h2>
            <ul>
              {unmapped.map((failure, index) => (
                <li key={`${failure.field}-${index}`} role="alert">
                  {failure.message}
                </li>
              ))}
            </ul>
          </div>
        </section>
      ) : null}

      {problem !== null ? (
        <p role="alert" className="alert alert--attention" data-testid="invoice-problem">
          {problem}
        </p>
      ) : null}

      <div className="form__field">
        <label htmlFor="invoice-custom-message">{t("mobile.invoice.custom_message")}</label>
        <textarea
          id="invoice-custom-message"
          rows={3}
          data-testid="invoice-custom-message"
          value={customMessage}
          onChange={(event) => setCustomMessage(event.target.value)}
        />
      </div>

      <div className="form__actions">
        <button type="submit" disabled={sending} data-testid="invoice-submit">
          {sending ? t("mobile.invoice.sending") : t("mobile.invoice.submit")}
        </button>
        <p className="caption">{t("invoice.new.irreversible")}</p>
      </div>
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
    <p role="alert" className="field-error" data-testid={testId}>
      {failure.message}
    </p>
  );
}
