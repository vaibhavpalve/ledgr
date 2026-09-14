/**
 * The mobile Send-invoice tab's calls to the API — MOB-005, FR-AR-001 ..
 * FR-AR-005.
 *
 * Follows `capture/api.ts` and `templates/api.ts`'s conventions exactly
 * (`ApiOptions`, `ApiError`, `OfflineError`, an idempotency key on every
 * mutating call, headers from `languageHeaders`) — the same kind of client
 * talking to the same kind of server.
 *
 * --- The four-call sequence lives in the FORM, not here ---
 *
 * This client exposes each of `create` / `set lines` / `issue` / `send` as
 * its own method, exactly as `api.invoicing.routes` exposes them as separate
 * endpoints. `SendInvoiceForm` is what sequences them — a client method that
 * secretly did all four would hide exactly the step a submit failed at from
 * whatever renders the error.
 *
 * --- Two distinct 422 shapes, and therefore two error types ---
 *
 * `create`'s `CustomerDetailsMissing`/`CustomerDetailsConflict` carry
 * `missing_fields` as a list of bare field-name STRINGS (there is one
 * translated sentence for the whole refusal, not one per field) —
 * `InvoiceCustomerProblemError`.
 *
 * `issue`'s `NotStatutoryCompliant` carries `missing_fields` shaped as
 * `{field, line_position, message}[]` — the SAME shape `_view_json`'s own
 * `statutory_failures` already uses — each with its own already-translated
 * sentence. `InvoiceNotCompliantError` carries these as `failures`, mirroring
 * `templates/api.ts`'s `TemplateNotCompliantError`.
 */

import type {
  SalesInvoiceStatus,
  SalesInvoiceSummaryView,
  SalesInvoiceView,
  StatutoryFailureView,
  VatTreatment,
} from "@ledgr/shared-types";
import type { Language } from "@ledgr/i18n";

import { languageHeaders } from "../i18n";

/** The call could not be made because there is no connection. */
export class OfflineError extends Error {}

/** See `capture/api.ts`'s identical class for the full rationale. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string | null,
    message: string,
  ) {
    super(message);
  }
}

/**
 * `invoice_customer_missing` / `invoice_customer_conflict` /
 * `customer_address_incomplete` — one combined sentence (`message`), plus
 * which typed fields the request was missing or should not have sent.
 */
export class InvoiceCustomerProblemError extends ApiError {
  constructor(
    status: number,
    reason: string,
    message: string,
    readonly fields: readonly string[],
  ) {
    super(status, reason, message);
  }
}

/** `sales_invoice_incomplete` (FR-AR-003's gate at issue) — see the module docstring. */
export class InvoiceNotCompliantError extends ApiError {
  constructor(
    status: number,
    message: string,
    readonly failures: readonly StatutoryFailureView[],
  ) {
    super(status, "sales_invoice_incomplete", message);
  }
}

export interface ApiOptions {
  language: () => Language;
  fetchImpl?: typeof fetch;
}

/** `api.invoicing.routes.LineBody` — amounts are decimal STRINGS, never numbers (NFR-031). */
export interface InvoiceLineBody {
  description: string;
  quantity: string;
  unit_price: string;
  vat_treatment: VatTreatment;
  discount_percent: string;
}

/**
 * `api.invoicing.routes.InvoiceBody`: EITHER `customer_id` (FR-AR-006's
 * master fills the snapshot in) OR the customer's details typed out — never
 * both, which the server refuses as `invoice_customer_conflict`. The typed
 * path was this form's MVP (ADR-046 §4); the picker path arrived with the
 * customers screen, and `SendInvoiceForm` sends exactly one of the two.
 */
export interface CreateInvoiceBody {
  fiscal_year_id: string;
  invoice_date: string;
  customer_id?: string | null;
  customer_name?: string | null;
  customer_address?: string | null;
  customer_country?: string | null;
  customer_vat_number?: string | null;
}

export interface SendInvoiceBody {
  channel?: string | null;
  to?: string | null;
}

export class SalesInvoiceApi {
  constructor(private readonly options: ApiOptions) {}

  /** MOB-005's View tab. `status` is not filterable server-side yet — see that route's own docstring. */
  listInvoices(administrationId: string): Promise<SalesInvoiceSummaryView[]> {
    return this.call<SalesInvoiceSummaryView[]>("GET", this.basePath(administrationId));
  }

  getInvoice(administrationId: string, invoiceId: string): Promise<SalesInvoiceView> {
    return this.call<SalesInvoiceView>("GET", this.invoicePath(administrationId, invoiceId));
  }

  /** Step 1 of MOB-005's four-call sequence: a draft, no lines yet. */
  createInvoice(administrationId: string, body: CreateInvoiceBody): Promise<SalesInvoiceView> {
    return this.call<SalesInvoiceView>("POST", this.basePath(administrationId), {
      ...body,
      lines: [],
    });
  }

  /** Step 2. */
  setInvoiceLines(
    administrationId: string,
    invoiceId: string,
    lines: readonly InvoiceLineBody[],
  ): Promise<SalesInvoiceView> {
    return this.call<SalesInvoiceView>(
      "PUT",
      `${this.invoicePath(administrationId, invoiceId)}/lines`,
      { lines },
    );
  }

  /** Step 3. */
  issueInvoice(administrationId: string, invoiceId: string): Promise<SalesInvoiceView> {
    return this.call<SalesInvoiceView>(
      "POST",
      `${this.invoicePath(administrationId, invoiceId)}/issue`,
    );
  }

  /** Step 4 — FR-AR-005. */
  sendInvoice(
    administrationId: string,
    invoiceId: string,
    body?: SendInvoiceBody,
  ): Promise<unknown> {
    return this.call("POST", `${this.invoicePath(administrationId, invoiceId)}/send`, body ?? {});
  }

  /**
   * MOB-004's "document viewable at full resolution", for an invoice's own
   * PDF. `fetch()` + `Blob` + a client-rendered `<img>`/`<embed>` via
   * `URL.createObjectURL`, never a direct `<img src="/v1/...">` — the exact
   * technique `templates/api.ts`'s `fetchTemplateAssetBlob` already
   * establishes, for the same SEC-005 reason (the download endpoint sends
   * `Content-Disposition: attachment` precisely so nothing navigates to it
   * directly).
   */
  async fetchDocumentBlob(administrationId: string, documentId: string): Promise<Blob> {
    const fetchImpl = this.options.fetchImpl ?? fetch;
    const path =
      `/v1/administrations/${encodeURIComponent(administrationId)}` +
      `/documents/${encodeURIComponent(documentId)}/content`;
    let response: Response;
    try {
      response = await fetchImpl(path, { headers: languageHeaders(this.options.language()) });
    } catch (cause) {
      throw new OfflineError(`GET ${path} could not reach the API`, { cause });
    }
    if (!response.ok) throw await problemFrom(response);
    return await response.blob();
  }

  private basePath(administrationId: string): string {
    return `/v1/administrations/${encodeURIComponent(administrationId)}/sales-invoices`;
  }

  private invoicePath(administrationId: string, invoiceId: string): string {
    return `${this.basePath(administrationId)}/${encodeURIComponent(invoiceId)}`;
  }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const mutating = method !== "GET";
    const fetchImpl = this.options.fetchImpl ?? fetch;

    let response: Response;
    try {
      response = await fetchImpl(path, {
        method,
        headers: {
          ...languageHeaders(this.options.language()),
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          // NFR-032. A fresh key per call: each is a new intention.
          ...(mutating ? { "Idempotency-Key": crypto.randomUUID() } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      throw new OfflineError(`${method} ${path} could not reach the API`, { cause });
    }

    if (!response.ok) throw await problemFrom(response);
    return (await response.json()) as T;
  }
}

const CUSTOMER_PROBLEM_REASONS = new Set([
  "invoice_customer_missing",
  "invoice_customer_conflict",
  "customer_address_incomplete",
]);

async function problemFrom(response: Response): Promise<ApiError> {
  let reason: string | null = null;
  let message = "";
  let rawMissingFields: unknown = null;
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail !== null && typeof detail === "object") {
      const fields = detail as { reason?: unknown; message?: unknown; missing_fields?: unknown };
      if (typeof fields.reason === "string") reason = fields.reason;
      if (typeof fields.message === "string") message = fields.message;
      rawMissingFields = fields.missing_fields ?? null;
    }
  } catch {
    // A gateway's HTML or an empty body. The status is still the answer.
  }

  const resolvedMessage = message || `HTTP ${response.status}`;

  if (reason === "sales_invoice_incomplete" && Array.isArray(rawMissingFields)) {
    return new InvoiceNotCompliantError(
      response.status,
      resolvedMessage,
      rawMissingFields as StatutoryFailureView[],
    );
  }

  if (reason !== null && CUSTOMER_PROBLEM_REASONS.has(reason) && Array.isArray(rawMissingFields)) {
    return new InvoiceCustomerProblemError(
      response.status,
      reason,
      resolvedMessage,
      rawMissingFields as string[],
    );
  }

  return new ApiError(response.status, reason, resolvedMessage);
}

/** Re-exported so a caller building a list row does not need `SalesInvoiceStatus` from elsewhere. */
export type { SalesInvoiceStatus };
