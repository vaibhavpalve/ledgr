import { describe, expect, it } from "vitest";

import { fakeFetch, jsonResponse, problemResponse } from "../testing/fakeFetch";
import { ApiError, CustomerApi, vatNumberFormatProblem, type CustomerBody } from "./api";

const body: CustomerBody = {
  name: "Bakkerij De Vries",
  trade_name: null,
  address_line1: "Dorpsstraat 1",
  address_line2: null,
  postal_code: "1234 AB",
  city: "Amersfoort",
  country: "NL",
  kvk_number: "28844196",
  vat_number: "NL001234567B01",
  peppol_participant_id: null,
  payment_terms_days: 30,
  credit_limit: "5000.00",
  delivery_channel: "email",
  invoice_email: "factuur@devries.nl",
  language: "nl",
  notes: null,
};

describe("CustomerApi", () => {
  it("passes the search query through to the server and never filters locally", async () => {
    const { impl, calls } = fakeFetch({
      "GET /v1/administrations/adm-A/customers": () => jsonResponse([]),
    });
    const api = new CustomerApi({ language: () => "nl", fetchImpl: impl });

    await api.listCustomers("adm-A", { q: "vries", includeArchived: true, limit: 20 });

    expect(calls[0]?.url).toBe(
      "/v1/administrations/adm-A/customers?q=vries&include_archived=true&limit=20",
    );
  });

  it("creates with the CustomerBody as-is — the credit limit stays a string", async () => {
    const { impl, calls } = fakeFetch({
      "POST /v1/administrations/adm-A/customers": (call) =>
        jsonResponse({ id: "c1", ...(call.body as object), vat_number_status: "unchecked" }),
    });
    const api = new CustomerApi({ language: () => "nl", fetchImpl: impl });

    const created = await api.createCustomer("adm-A", body);

    expect(created.id).toBe("c1");
    expect((calls[0]?.body as CustomerBody).credit_limit).toBe("5000.00");
    expect(calls[0]?.headers["idempotency-key"]).toBeTruthy();
  });

  it("updates with PUT, archives and validates through their own routes", async () => {
    const { impl, calls } = fakeFetch({
      "PUT /v1/administrations/adm-A/customers/c1": () => jsonResponse({ id: "c1" }),
      "POST /v1/administrations/adm-A/customers/c1/archive": () =>
        jsonResponse({ id: "c1", archived_at: "2026-09-14T10:00:00Z" }),
      "POST /v1/administrations/adm-A/customers/c1/vat-number/validate": () =>
        jsonResponse({ id: "c1", vat_number_status: "valid" }),
    });
    const api = new CustomerApi({ language: () => "nl", fetchImpl: impl });

    await api.updateCustomer("adm-A", "c1", body);
    await api.archiveCustomer("adm-A", "c1");
    const validated = await api.validateVatNumber("adm-A", "c1");

    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
      "PUT /v1/administrations/adm-A/customers/c1",
      "POST /v1/administrations/adm-A/customers/c1/archive",
      "POST /v1/administrations/adm-A/customers/c1/vat-number/validate",
    ]);
    expect(validated.vat_number_status).toBe("valid");
  });

  it("keeps the field a 422 names, so a form can mark the right input", async () => {
    const { impl } = fakeFetch({
      "POST /v1/administrations/adm-A/customers": () =>
        problemResponse(422, "customer_field_invalid", "Dit gegeven klopt niet.", {
          field: "credit_limit",
        }),
    });
    const api = new CustomerApi({ language: () => "nl", fetchImpl: impl });

    const error = (await api.createCustomer("adm-A", body).catch((e: unknown) => e)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.fields.field).toBe("credit_limit");
  });
});

describe("vatNumberFormatProblem — format feedback only, never a VIES verdict", () => {
  it("accepts a well-formed Dutch number, with or without spacing", () => {
    expect(vatNumberFormatProblem("NL001234567B01")).toBeNull();
    expect(vatNumberFormatProblem("nl 0012 34567 b01")).toBeNull();
  });

  it("flags a Dutch number of the wrong shape", () => {
    expect(vatNumberFormatProblem("NL12345B01")).toBe("malformed");
    expect(vatNumberFormatProblem("NL001234567")).toBe("malformed");
  });

  it("accepts another member state's envelope and flags noise", () => {
    expect(vatNumberFormatProblem("DE123456789")).toBeNull();
    expect(vatNumberFormatProblem("123")).toBe("malformed");
    expect(vatNumberFormatProblem("")).toBe("empty");
  });
});
