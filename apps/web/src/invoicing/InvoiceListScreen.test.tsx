import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@ledgr/i18n";
import type { SalesInvoiceSummaryView } from "@ledgr/shared-types";

import { InvoiceListScreen } from "./InvoiceListScreen";

/**
 * The list a bookkeeper scans: where each invoice stands with the money, what
 * it is worth, its PDF, how it was sent, when it was paid - and the views and
 * the draft delete built on those.
 */

const base: SalesInvoiceSummaryView = {
  id: "x",
  status: "issued",
  invoice_number: 1,
  invoice_reference: "26700014",
  invoice_date: "2026-07-20",
  due_date: "2026-08-19",
  customer_name: "Allegis Group (Switzerland) GmbH",
  customer_id: null,
  document_id: "doc",
  gross_amount: "13902.00",
  payment_status: "overdue",
  payment_date: null,
  send_channel: "email",
  send_status: "sent",
  attachment_name: "26700014.pdf",
};

const overdue = { ...base, id: "overdue-1" };
const paid = {
  ...base,
  id: "paid-1",
  invoice_reference: "26700015",
  payment_status: "paid" as const,
  payment_date: "2026-08-25",
  attachment_name: "26700015.pdf",
};
const draft: SalesInvoiceSummaryView = {
  ...base,
  id: "draft-1",
  status: "draft",
  invoice_number: null,
  invoice_reference: null,
  document_id: null,
  gross_amount: "16632.00",
  payment_status: "draft",
  send_channel: null,
  send_status: null,
  attachment_name: null,
};

const services = {
  invoices: {
    listInvoices: vi.fn(async () => [overdue, paid, draft]),
    discardInvoice: vi.fn(async () => ({ status: "discarded" })),
  },
};

vi.mock("../session/SessionProvider", () => ({
  useAdministration: () => ({ administration: { id: "adm-A" } }),
}));
vi.mock("../session/ServicesProvider", () => ({ useServices: () => services }));

async function open() {
  render(
    <I18nProvider initialLanguage="en">
      <MemoryRouter>
        <InvoiceListScreen />
      </MemoryRouter>
    </I18nProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("invoice-table")).not.toBeNull());
}

const rowOf = (text: string) => screen.getByText(text).closest("tr") as HTMLElement;

describe("the sales invoice list", () => {
  it("shows status, date, number, customer, amount, attachment, due date, send method and payment date", async () => {
    await open();
    const headings = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headings.slice(0, 9)).toEqual([
      "Status",
      "Invoice date",
      "Number",
      "Customer",
      "Amount",
      "Attachment",
      "Due date",
      "Send method",
      "Payment date",
    ]);

    const late = within(rowOf("26700014"));
    expect(late.getByTestId("invoice-status").textContent).toBe("Overdue");
    expect(late.getByText("26700014.pdf")).not.toBeNull();
    expect(late.getByTestId("invoice-send-method").textContent).toContain("Email");
    expect(late.getByTestId("invoice-payment-date").textContent).toBe("—");
    expect(late.getByTestId("invoice-amount").textContent?.replace(" ", " ")).toBe("€ 13.902,00");

    const settled = within(rowOf("26700015"));
    expect(settled.getByTestId("invoice-status").textContent).toBe("Paid");
    expect(settled.getByTestId("invoice-payment-date").textContent).toBe("25-08-2026");
  });

  it("counts the overdue and draft invoices in the tabs and filters by them", async () => {
    await open();
    expect(screen.getByTestId("invoice-tab-overdue").textContent).toBe("Overdue (1)");
    expect(screen.getByTestId("invoice-tab-draft").textContent).toBe("Drafts (1)");
    expect(screen.getAllByTestId("invoice-row")).toHaveLength(3);

    fireEvent.click(screen.getByTestId("invoice-tab-overdue"));
    expect(screen.getAllByTestId("invoice-row")).toHaveLength(1);
    expect(screen.queryByText("26700015")).toBeNull();

    fireEvent.click(screen.getByTestId("invoice-tab-draft"));
    expect(screen.getAllByTestId("invoice-row")).toHaveLength(1);
    expect(screen.getByText("16.632,00", { exact: false })).not.toBeNull();
  });

  it("offers delete on a draft only, and only after a second confirming click", async () => {
    await open();
    expect(screen.queryByTestId("invoice-delete-overdue-1")).toBeNull();
    expect(screen.queryByTestId("invoice-delete-paid-1")).toBeNull();

    fireEvent.click(screen.getByTestId("invoice-delete-draft-1"));
    expect(services.invoices.discardInvoice).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("invoice-delete-confirm-draft-1"));
    await waitFor(() =>
      expect(services.invoices.discardInvoice).toHaveBeenCalledWith("adm-A", "draft-1"),
    );
    await waitFor(() => expect(screen.getAllByTestId("invoice-row")).toHaveLength(2));
  });

  it("shows a problem and keeps the draft when the server refuses the delete", async () => {
    services.invoices.discardInvoice.mockRejectedValueOnce(new Error("refused"));
    await open();
    fireEvent.click(screen.getByTestId("invoice-delete-draft-1"));
    fireEvent.click(screen.getByTestId("invoice-delete-confirm-draft-1"));

    await waitFor(() => expect(screen.getByTestId("invoice-delete-problem")).not.toBeNull());
    expect(screen.getAllByTestId("invoice-row")).toHaveLength(3);
  });
});
