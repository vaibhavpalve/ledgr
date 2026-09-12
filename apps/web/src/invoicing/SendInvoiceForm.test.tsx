import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { InvoiceCustomerProblemError, InvoiceNotCompliantError, type SalesInvoiceApi } from "./api";
import { SendInvoiceForm } from "./SendInvoiceForm";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

const draft = { id: "inv-1", status: "draft" };

function api(overrides: Partial<SalesInvoiceApi> = {}): SalesInvoiceApi {
  return {
    createInvoice: vi.fn(async () => draft),
    setInvoiceLines: vi.fn(async () => draft),
    issueInvoice: vi.fn(async () => ({ ...draft, status: "issued" })),
    sendInvoice: vi.fn(async () => ({ channel: "email" })),
    ...overrides,
  } as unknown as SalesInvoiceApi;
}

function fillCustomer() {
  fireEvent.change(screen.getByTestId("invoice-customer-name"), {
    target: { value: "Acme B.V." },
  });
  fireEvent.change(screen.getByTestId("invoice-customer-address"), {
    target: { value: "Damrak 1, Amsterdam" },
  });
}

describe("SendInvoiceForm — MOB-005's create/send flow", () => {
  it("calls create, set-lines, issue and send in order with the right payload shapes", async () => {
    const invoiceApi = api();
    render(<SendInvoiceForm administrationId="adm-A" fiscalYearId="fy-1" api={invoiceApi} />);

    fillCustomer();
    fireEvent.change(screen.getByTestId("invoice-line-1-description"), {
      target: { value: "Consultancy" },
    });
    fireEvent.change(screen.getByTestId("invoice-line-1-unit-price"), {
      target: { value: "100.00" },
    });

    fireEvent.click(screen.getByTestId("invoice-submit"));

    await waitFor(() => expect(screen.getByTestId("invoice-sent")).toBeDefined());

    expect(invoiceApi.createInvoice).toHaveBeenCalledWith(
      "adm-A",
      expect.objectContaining({
        fiscal_year_id: "fy-1",
        customer_name: "Acme B.V.",
        customer_address: "Damrak 1, Amsterdam",
      }),
    );
    expect(invoiceApi.setInvoiceLines).toHaveBeenCalledWith(
      "adm-A",
      "inv-1",
      expect.arrayContaining([
        expect.objectContaining({
          description: "Consultancy",
          quantity: "1",
          unit_price: "100.00",
          vat_treatment: "btw_21",
        }),
      ]),
    );
    expect(invoiceApi.issueInvoice).toHaveBeenCalledWith("adm-A", "inv-1");
    expect(invoiceApi.sendInvoice).toHaveBeenCalledWith("adm-A", "inv-1");

    // Order matters: create before lines before issue before send.
    const order = [
      vi.mocked(invoiceApi.createInvoice).mock.invocationCallOrder[0],
      vi.mocked(invoiceApi.setInvoiceLines).mock.invocationCallOrder[0],
      vi.mocked(invoiceApi.issueInvoice).mock.invocationCallOrder[0],
      vi.mocked(invoiceApi.sendInvoice).mock.invocationCallOrder[0],
    ];
    for (const callOrder of order) expect(callOrder).toBeDefined();
    expect(order).toEqual([...order].sort((a = 0, b = 0) => a - b));
  });

  it("renders a create-step customer refusal at the customer section, not a generic banner", async () => {
    const invoiceApi = api({
      createInvoice: vi.fn(async () => {
        throw new InvoiceCustomerProblemError(
          422,
          "invoice_customer_missing",
          "Een factuur heeft een naam en adres nodig.",
          ["customer_name", "customer_address"],
        );
      }),
    });
    render(<SendInvoiceForm administrationId="adm-A" fiscalYearId="fy-1" api={invoiceApi} />);

    fireEvent.click(screen.getByTestId("invoice-submit"));

    await waitFor(() => expect(screen.getByTestId("invoice-customer-problem")).toBeDefined());
    expect(screen.getByTestId("invoice-customer-problem").textContent).toContain("naam en adres");
    expect(screen.queryByTestId("invoice-problem")).toBeNull();
  });

  it("renders an issue-step statutory failure inline at its own field, not a generic banner", async () => {
    const invoiceApi = api({
      issueInvoice: vi.fn(async () => {
        throw new InvoiceNotCompliantError(422, "Deze factuur ontbreekt gegevens.", [
          { field: "customer_vat_number", line_position: null, message: "BTW-nummer ontbreekt." },
          {
            field: "line_description",
            line_position: 1,
            message: "Regel 1 mist een omschrijving.",
          },
        ]);
      }),
    });
    render(<SendInvoiceForm administrationId="adm-A" fiscalYearId="fy-1" api={invoiceApi} />);

    fillCustomer();
    fireEvent.click(screen.getByTestId("invoice-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("invoice-field-customer_vat_number")).toBeDefined(),
    );
    expect(screen.getByTestId("invoice-field-customer_vat_number").textContent).toBe(
      "BTW-nummer ontbreekt.",
    );
    expect(screen.getByTestId("invoice-line-1-description-problem").textContent).toBe(
      "Regel 1 mist een omschrijving.",
    );
    expect(screen.queryByTestId("invoice-problem")).toBeNull();
  });

  it("does not create a second draft when retrying after a later-step failure", async () => {
    const invoiceApi = api({
      setInvoiceLines: vi
        .fn()
        .mockRejectedValueOnce(new Error("network blip"))
        .mockResolvedValueOnce(draft),
    });
    render(<SendInvoiceForm administrationId="adm-A" fiscalYearId="fy-1" api={invoiceApi} />);

    fillCustomer();
    fireEvent.click(screen.getByTestId("invoice-submit"));
    await waitFor(() => expect(invoiceApi.createInvoice).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByTestId("invoice-submit"));
    await waitFor(() => expect(screen.getByTestId("invoice-sent")).toBeDefined());

    expect(invoiceApi.createInvoice).toHaveBeenCalledTimes(1);
  });
});
