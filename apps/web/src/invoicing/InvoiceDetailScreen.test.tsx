import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@ledgr/i18n";
import type { SalesInvoiceView } from "@ledgr/shared-types";

import { InvoiceDetailScreen } from "./InvoiceDetailScreen";
import { formatQuantity, formatUnitPrice, trimDecimal } from "./amounts";

/**
 * The bug this pins: `unit_price` and `quantity` are numeric(19,4), so an
 * invoice line arrives as `"100.0000"`; `money()` refuses more than two places,
 * threw during render and left the detail page blank - a draft "would not open".
 */

const invoice = {
  id: "inv-1",
  status: "draft",
  invoice_number: null,
  invoice_reference: null,
  invoice_date: "2026-09-19",
  supply_date: null,
  due_date: "2026-10-19",
  customer_name: "Allegis Group (Switzerland) GmbH",
  customer_address: "Zurich",
  customer_country: "CH",
  customer_vat_number: null,
  customer_id: null,
  customer_language: "en",
  credits_invoice_id: null,
  journal_entry_id: null,
  document_id: null,
  notes: null,
  issued_at: null,
  lines: [
    {
      id: "l1",
      position: 1,
      description: "Consulting",
      quantity: "1.0000",
      unit_price: "100.0000",
      discount_percent: "0.0000",
      vat_treatment: "btw_21",
      line_net: "100.00",
    },
  ],
  vat_groups: [
    {
      vat_treatment: "btw_21",
      role: "standard",
      rate: "21",
      taxable_amount: "100.00",
      vat_amount: "21.00",
      legal_wording: null,
    },
  ],
  net_amount: "100.00",
  vat_amount: "21.00",
  gross_amount: "121.00",
  statutory_failures: [],
} as unknown as SalesInvoiceView;

vi.mock("../session/SessionProvider", () => ({
  useAdministration: () => ({ administration: { id: "adm-A" } }),
}));
// One stable object: a fresh one per render would change the screen's effect
// dependencies every time and it would never leave its loading state.
const services = { invoices: { getInvoice: vi.fn(async () => invoice) } };
vi.mock("../session/ServicesProvider", () => ({ useServices: () => services }));

describe("the invoice detail screen", () => {
  it("opens a draft whose lines carry four-decimal quantity and unit price", async () => {
    render(
      <I18nProvider initialLanguage="en">
        <MemoryRouter initialEntries={["/invoices/inv-1"]}>
          <Routes>
            <Route path="/invoices/:invoiceId" element={<InvoiceDetailScreen />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("invoice-detail")).not.toBeNull());
    const row = screen.getByText("Consulting").closest("tr") as HTMLElement;
    const cells = Array.from(row.querySelectorAll("td")).map((c) =>
      (c.textContent ?? "").replace(" ", " "),
    );
    expect(cells[1]).toBe("1");
    expect(cells[2]).toBe("€ 100,00");
  });
});

describe("trailing-zero trimming", () => {
  const formatters = {
    money: (v: string) => `M(${v})`,
    number: (v: string, { scale }: { scale: number }) => `N(${v}|${scale})`,
  };

  it("never rounds: it drops zeros only", () => {
    expect(trimDecimal("100.0000", 2)).toEqual({ text: "100.00", scale: 2 });
    expect(trimDecimal("99.9950", 2)).toEqual({ text: "99.995", scale: 3 });
    expect(trimDecimal("99.9951", 2)).toEqual({ text: "99.9951", scale: 4 });
    expect(trimDecimal("5", 2)).toEqual({ text: "5.00", scale: 2 });
    expect(trimDecimal("-1.5000", 2)).toEqual({ text: "-1.50", scale: 2 });
  });

  it("formats a two-place price as money and a real four-place price at its own scale", () => {
    expect(formatUnitPrice("100.0000", formatters)).toBe("M(100.00)");
    expect(formatUnitPrice("99.9951", formatters)).toBe("€ N(99.9951|4)");
  });

  it("shows a whole quantity without places and keeps a fractional one exact", () => {
    expect(formatQuantity("1.0000", formatters)).toBe("N(1|0)");
    expect(formatQuantity("2.5000", formatters)).toBe("N(2.5|1)");
    expect(formatQuantity("0.1250", formatters)).toBe("N(0.125|3)");
  });
});
