/**
 * The customer master's calls — FR-AR-006, FR-ONB-003 — against
 * `apps/api/src/api/customers/routes.py` exactly:
 *
 *   POST /v1/administrations/{id}/customers
 *   GET  /v1/administrations/{id}/customers?q=&include_archived=&limit=
 *   GET  /v1/administrations/{id}/customers/{customer_id}
 *   PUT  /v1/administrations/{id}/customers/{customer_id}       (whole form, not a patch)
 *   POST .../customers/{customer_id}/archive | /restore
 *   POST .../customers/{customer_id}/vat-number/validate       (always 200; the verdict is a field)
 *
 * `credit_limit` crosses the wire as a STRING (NFR-031) — never a number,
 * which pydantic would have already coerced through a float. The response
 * shape is `@ledgr/shared-types`' `CustomerView`, `_customer_json` field for
 * field.
 *
 * Searching is the SERVER's (FR-FRM-000's exact → prefix → substring
 * ordering); this client passes `q` through and never filters a wider list.
 */

import type { CustomerView, DeliveryChannel } from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

/** `api.customers.routes.CustomerBody` — one body for create and update. */
export interface CustomerBody {
  name: string;
  trade_name: string | null;
  address_line1: string | null;
  address_line2: string | null;
  postal_code: string | null;
  city: string | null;
  country: string;
  kvk_number: string | null;
  vat_number: string | null;
  peppol_participant_id: string | null;
  payment_terms_days: number;
  /** Decimal STRING or null — see the module docstring. */
  credit_limit: string | null;
  delivery_channel: DeliveryChannel;
  invoice_email: string | null;
  language: string;
  notes: string | null;
}

export class CustomerApi {
  constructor(private readonly options: ApiOptions) {}

  listCustomers(
    administrationId: string,
    params: { q?: string; includeArchived?: boolean; limit?: number } = {},
  ): Promise<CustomerView[]> {
    return callJson<CustomerView[]>(
      this.options,
      "GET",
      this.basePath(administrationId) +
        queryOf({
          q: params.q,
          include_archived: params.includeArchived ? "true" : undefined,
          limit: params.limit,
        }),
    );
  }

  getCustomer(administrationId: string, customerId: string): Promise<CustomerView> {
    return callJson<CustomerView>(this.options, "GET", this.customerPath(administrationId, customerId));
  }

  createCustomer(administrationId: string, body: CustomerBody): Promise<CustomerView> {
    return callJson<CustomerView>(this.options, "POST", this.basePath(administrationId), body);
  }

  /** PUT: the customer is edited as one form and sent back whole. */
  updateCustomer(administrationId: string, customerId: string, body: CustomerBody): Promise<CustomerView> {
    return callJson<CustomerView>(
      this.options,
      "PUT",
      this.customerPath(administrationId, customerId),
      body,
    );
  }

  archiveCustomer(administrationId: string, customerId: string): Promise<CustomerView> {
    return callJson<CustomerView>(
      this.options,
      "POST",
      `${this.customerPath(administrationId, customerId)}/archive`,
    );
  }

  restoreCustomer(administrationId: string, customerId: string): Promise<CustomerView> {
    return callJson<CustomerView>(
      this.options,
      "POST",
      `${this.customerPath(administrationId, customerId)}/restore`,
    );
  }

  /** FR-ONB-003: consult VIES on demand. Always 200 — read `vat_number_status` on the answer. */
  validateVatNumber(administrationId: string, customerId: string): Promise<CustomerView> {
    return callJson<CustomerView>(
      this.options,
      "POST",
      `${this.customerPath(administrationId, customerId)}/vat-number/validate`,
    );
  }

  private basePath(administrationId: string): string {
    return pathOf("v1", "administrations", administrationId, "customers");
  }

  private customerPath(administrationId: string, customerId: string): string {
    return pathOf("v1", "administrations", administrationId, "customers", customerId);
  }
}

/**
 * A Dutch VAT identification number's shape — `NL` + 9 digits + `B` + 2
 * digits — for the inline hint while typing. This is FORMAT feedback only:
 * the verdict on whether the number exists is VIES's (`vat_number_status`),
 * never this function's, and a well-formed number that VIES rejects is
 * shown as such by the screen.
 */
export function vatNumberFormatProblem(raw: string): "empty" | "malformed" | null {
  const value = raw.replace(/[\s.-]/g, "").toUpperCase();
  if (value === "") return "empty";
  if (value.startsWith("NL")) return /^NL\d{9}B\d{2}$/.test(value) ? null : "malformed";
  // Another member state: two letters then 2–13 alphanumerics is the EU-wide envelope.
  return /^[A-Z]{2}[A-Z0-9]{2,13}$/.test(value) ? null : "malformed";
}
