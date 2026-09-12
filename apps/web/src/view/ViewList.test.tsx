import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { ExpenseSummaryView, SalesInvoiceSummaryView } from "@ledgr/shared-types";

import type { CaptureApi } from "../capture/api";
import type { SalesInvoiceApi } from "../invoicing/api";
import { ViewList } from "./ViewList";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

// jsdom does not implement the Blob URL API at all - see
// TemplateDesigner.test.tsx's identical stub, for the same reason:
// DocumentViewer calls URL.createObjectURL/revokeObjectURL on the fetched blob.
beforeAll(() => {
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:mock-document-url"),
      revokeObjectURL: vi.fn(),
    }),
  );
});

const readyExpense: ExpenseSummaryView = {
  id: "exp-1",
  status: "ready",
  capture_item_id: "item-1",
  expense_date: "2026-09-01",
  supplier: "Café Central",
  gross_amount: "12.50",
  category: "Representatie",
  missing_fields: [],
  can_be_marked_ready: true,
};

const draftExpense: ExpenseSummaryView = { ...readyExpense, id: "exp-2", status: "draft" };

const issuedInvoice: SalesInvoiceSummaryView = {
  id: "inv-1",
  status: "issued",
  invoice_number: 42,
  invoice_reference: "2026-0042",
  invoice_date: "2026-09-01",
  due_date: "2026-10-01",
  customer_name: "Acme B.V.",
  customer_id: null,
  document_id: "doc-1",
};

function captureApi(overrides: Partial<CaptureApi> = {}): CaptureApi {
  return {
    listExpenses: vi.fn(async () => [readyExpense, draftExpense]),
    ...overrides,
  } as unknown as CaptureApi;
}

function invoiceApi(overrides: Partial<SalesInvoiceApi> = {}): SalesInvoiceApi {
  return {
    listInvoices: vi.fn(async () => [issuedInvoice]),
    fetchDocumentBlob: vi.fn(async () => new Blob(["%PDF-1.4"], { type: "application/pdf" })),
    ...overrides,
  } as unknown as SalesInvoiceApi;
}

describe("ViewList — MOB-004's read-only View tab", () => {
  it("shows posted/ready expenses and excludes drafts", async () => {
    render(
      <ViewList administrationId="adm-A" captureApi={captureApi()} invoiceApi={invoiceApi()} />,
    );

    await waitFor(() => expect(screen.getByTestId("view-expenses-list")).toBeDefined());
    expect(screen.getByTestId("view-expense-exp-1")).toBeDefined();
    expect(screen.queryByTestId("view-expense-exp-2")).toBeNull();
  });

  it("shows issued (and draft) sales invoices", async () => {
    render(
      <ViewList administrationId="adm-A" captureApi={captureApi()} invoiceApi={invoiceApi()} />,
    );

    await waitFor(() => expect(screen.getByTestId("view-invoices-list")).toBeDefined());
    expect(screen.getByTestId("view-invoice-inv-1").textContent).toContain("Acme B.V.");
  });

  it("shows empty states when there is nothing to view", async () => {
    render(
      <ViewList
        administrationId="adm-A"
        captureApi={captureApi({ listExpenses: vi.fn(async () => []) })}
        invoiceApi={invoiceApi({ listInvoices: vi.fn(async () => []) })}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("view-expenses-empty")).toBeDefined());
    expect(screen.getByTestId("view-invoices-empty")).toBeDefined();
  });

  it("opening an invoice with a document fetches it as a Blob and renders it, never a direct src to the API", async () => {
    const invApi = invoiceApi();
    render(<ViewList administrationId="adm-A" captureApi={captureApi()} invoiceApi={invApi} />);

    await waitFor(() => expect(screen.getByTestId("view-invoice-inv-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("view-invoice-inv-1"));

    await waitFor(() => expect(screen.getByTestId("view-document-frame")).toBeDefined());
    expect(invApi.fetchDocumentBlob).toHaveBeenCalledWith("adm-A", "doc-1");
    const src = screen.getByTestId("view-document-frame").getAttribute("src");
    expect(src).toMatch(/^blob:/);
  });

  it("opening an expense (no document link yet) shows the documented gap rather than crashing", async () => {
    render(
      <ViewList administrationId="adm-A" captureApi={captureApi()} invoiceApi={invoiceApi()} />,
    );

    await waitFor(() => expect(screen.getByTestId("view-expense-exp-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("view-expense-exp-1"));

    expect(screen.getByTestId("view-document-none")).toBeDefined();
  });

  it("the back button returns from the detail view to the lists", async () => {
    render(
      <ViewList administrationId="adm-A" captureApi={captureApi()} invoiceApi={invoiceApi()} />,
    );

    await waitFor(() => expect(screen.getByTestId("view-invoice-inv-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("view-invoice-inv-1"));
    await waitFor(() => expect(screen.getByTestId("view-back")).toBeDefined());

    fireEvent.click(screen.getByTestId("view-back"));
    expect(screen.getByTestId("view-invoices-list")).toBeDefined();
  });
});
