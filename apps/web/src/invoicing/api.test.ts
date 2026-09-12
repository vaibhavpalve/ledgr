import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  InvoiceCustomerProblemError,
  InvoiceNotCompliantError,
  SalesInvoiceApi,
} from "./api";

function respond(status: number, body?: unknown): typeof fetch {
  return vi.fn(async () =>
    body === undefined
      ? new Response(null, { status })
      : new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
  ) as unknown as typeof fetch;
}

function client(fetchImpl: typeof fetch): SalesInvoiceApi {
  return new SalesInvoiceApi({ language: () => "nl", fetchImpl });
}

const invoiceView = { id: "inv-1", status: "draft" };

describe("SalesInvoiceApi", () => {
  it("lists invoices with a plain GET", async () => {
    const fetchImpl = respond(200, [invoiceView]);
    const result = await client(fetchImpl).listInvoices("adm-A");

    expect(result).toEqual([invoiceView]);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/sales-invoices",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("creates a draft with POST, an idempotency key, and empty lines", async () => {
    const fetchImpl = respond(200, invoiceView);
    await client(fetchImpl).createInvoice("adm-A", {
      fiscal_year_id: "fy-1",
      invoice_date: "2026-09-12",
      customer_name: "Acme B.V.",
      customer_address: "Damrak 1",
      customer_country: "NL",
    });

    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/sales-invoices",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          fiscal_year_id: "fy-1",
          invoice_date: "2026-09-12",
          customer_name: "Acme B.V.",
          customer_address: "Damrak 1",
          customer_country: "NL",
          lines: [],
        }),
      }),
    );
    const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
  });

  it("sets lines with PUT", async () => {
    const fetchImpl = respond(200, invoiceView);
    await client(fetchImpl).setInvoiceLines("adm-A", "inv-1", [
      {
        description: "Consultancy",
        quantity: "1",
        unit_price: "100.00",
        vat_treatment: "btw_21",
        discount_percent: "0",
      },
    ]);

    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/sales-invoices/inv-1/lines",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("issues with POST and no body", async () => {
    const fetchImpl = respond(200, invoiceView);
    await client(fetchImpl).issueInvoice("adm-A", "inv-1");

    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/sales-invoices/inv-1/issue",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("sends with POST", async () => {
    const fetchImpl = respond(200, { channel: "email" });
    await client(fetchImpl).sendInvoice("adm-A", "inv-1");

    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/sales-invoices/inv-1/send",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("fetches a document as a Blob rather than a JSON body", async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(new Blob(["%PDF-1.4"]), {
          status: 200,
          headers: { "Content-Type": "application/pdf" },
        }),
    ) as unknown as typeof fetch;

    const blob = await client(fetchImpl).fetchDocumentBlob("adm-A", "doc-1");

    expect(blob).toBeInstanceOf(Blob);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/documents/doc-1/content",
      expect.objectContaining({ headers: expect.any(Object) }),
    );
  });

  it("surfaces a create-step customer refusal with its raw field names", async () => {
    const fetchImpl = respond(422, {
      detail: {
        reason: "invoice_customer_missing",
        message: "Een factuur heeft een naam en adres nodig.",
        missing_fields: ["customer_name", "customer_address"],
      },
    });

    let caught: unknown;
    try {
      await client(fetchImpl).createInvoice("adm-A", {
        fiscal_year_id: "fy-1",
        invoice_date: "2026-09-12",
        customer_name: "",
        customer_address: "",
        customer_country: "NL",
      });
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(InvoiceCustomerProblemError);
    expect((caught as InvoiceCustomerProblemError).fields).toEqual([
      "customer_name",
      "customer_address",
    ]);
  });

  it("surfaces an issue-step statutory refusal with per-field, per-line failures", async () => {
    const fetchImpl = respond(422, {
      detail: {
        reason: "sales_invoice_incomplete",
        message: "Deze factuur ontbreekt wettelijk verplichte gegevens.",
        missing_fields: [
          { field: "customer_vat_number", line_position: null, message: "BTW-nummer ontbreekt." },
          {
            field: "line_description",
            line_position: 1,
            message: "Regel 1 mist een omschrijving.",
          },
        ],
      },
    });

    let caught: unknown;
    try {
      await client(fetchImpl).issueInvoice("adm-A", "inv-1");
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(InvoiceNotCompliantError);
    const failures = (caught as InvoiceNotCompliantError).failures;
    expect(failures).toHaveLength(2);
    expect(failures[1]).toEqual({
      field: "line_description",
      line_position: 1,
      message: "Regel 1 mist een omschrijving.",
    });
  });

  it("falls back to a plain ApiError for a refusal with no field data", async () => {
    const fetchImpl = respond(409, {
      detail: { reason: "sales_invoice_issued", message: "Deze factuur is al verstuurd." },
    });

    let caught: unknown;
    try {
      await client(fetchImpl).setInvoiceLines("adm-A", "inv-1", []);
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).not.toBeInstanceOf(InvoiceNotCompliantError);
    expect(caught).not.toBeInstanceOf(InvoiceCustomerProblemError);
  });
});
