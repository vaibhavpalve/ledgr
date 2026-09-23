import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { DashboardView, SalesInvoiceSummaryView } from "@ledgr/shared-types";

import type { SalesInvoiceApi } from "../invoicing/api";
import { axeViolations } from "../testing/axe";
import type { DashboardApi } from "./api";
import { HomeScreen } from "./HomeScreen";

/**
 * The Home redesign (design/reference/Home.png, Home-empty.png; ADR-080): what
 * it does with real data and what it refuses to invent.
 */

function render(ui: ReactElement, language: Language = "en") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

const base: DashboardView = {
  cash_position: "24310.45",
  receivables: "12850.00",
  vat_estimate: "4216.80",
  vat_period_start: "2026-07-01",
  vat_period_end: "2026-09-30",
  items_needing_action: [],
};
const brandNew: DashboardView = {
  ...base,
  cash_position: "0.00",
  receivables: "0.00",
  vat_estimate: "0.00",
};

const invoice = (id: string, date: string, ref: string | null): SalesInvoiceSummaryView => ({
  id,
  status: ref ? "issued" : "draft",
  invoice_number: null,
  invoice_reference: ref,
  invoice_date: date,
  due_date: null,
  customer_name: "Studio Noord",
  customer_id: null,
  document_id: null,
  gross_amount: null,
  payment_status: ref ? "open" : "draft",
  payment_date: null,
  send_channel: null,
  send_status: null,
  attachment_name: null,
});

function home(
  summary: DashboardView,
  options: {
    invoices?: SalesInvoiceSummaryView[];
    invoicesFail?: boolean;
    onNavigate?: (path: string) => void;
  } = {},
) {
  const dashboard = { getDashboard: vi.fn(async () => summary) } as unknown as DashboardApi;
  const invoicesApi = {
    listInvoices: vi.fn(async () => {
      if (options.invoicesFail) throw new Error("boom");
      return options.invoices ?? [];
    }),
  } as unknown as SalesInvoiceApi;
  return render(
    <HomeScreen
      administrationId="adm-A"
      fiscalYearId="fy"
      api={dashboard}
      invoicesApi={invoicesApi}
      companyName="Datapal BV"
      onOpenItem={vi.fn()}
      onNavigate={options.onNavigate}
    />,
  );
}

describe("a company with no bookings", () => {
  it("shows the four-step setup checklist instead of the attention list", async () => {
    home(brandNew);
    await waitFor(() => expect(screen.getByTestId("home-setup")).not.toBeNull());
    expect(screen.getByText("Set up Datapal BV")).not.toBeNull();
    expect(screen.queryByTestId("home-items-list")).toBeNull();
    expect(screen.queryByTestId("home-items-empty")).toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
  });

  it("shows zero in the mono figure style with a helpful caption and no delta chip", async () => {
    home(brandNew);
    await waitFor(() => expect(screen.getByTestId("home-cash-position")).not.toBeNull());
    // The formatter separates the symbol with a non-breaking space.
    expect(screen.getByTestId("home-cash-position").textContent?.replace("\u00a0", " ")).toBe(
      "€ 0,00",
    );
    expect(screen.getByTestId("home-cash-position-caption").textContent).toBe(
      "Appears after your first booking.",
    );
    expect(document.querySelector(".ui-badge--delta")).toBeNull();
    expect(screen.getByTestId("home-chart-empty")).not.toBeNull();
  });

  it("sends each step to its own screen, with one primary action", async () => {
    const onNavigate = vi.fn();
    home(brandNew, { onNavigate });
    await waitFor(() => expect(screen.getByTestId("home-setup-customer")).not.toBeNull());

    expect(screen.getByTestId("home-setup-customer").classList.contains("ui-btn--primary")).toBe(
      true,
    );
    expect(screen.getByTestId("home-setup-invoice").classList.contains("ui-btn--primary")).toBe(
      false,
    );
    fireEvent.click(screen.getByTestId("home-setup-invoice"));
    expect(onNavigate).toHaveBeenCalledWith("/invoices/new");
    fireEvent.click(screen.getByTestId("home-setup-capture"));
    expect(onNavigate).toHaveBeenCalledWith("/purchases");
  });

  it("is not the setup state once a single figure is non-zero", async () => {
    home({ ...brandNew, receivables: "0.01" });
    await waitFor(() => expect(screen.getByTestId("home-items-empty")).not.toBeNull());
    expect(screen.queryByTestId("home-setup")).toBeNull();
  });

  it("has no automated WCAG violations (CMP-012)", async () => {
    const { container } = home(brandNew);
    await waitFor(() => expect(screen.getByTestId("home-setup")).not.toBeNull());
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("a company with bookings", () => {
  it("shows the BTW period as a quarter when it is exactly one", async () => {
    home(base);
    await waitFor(() => expect(screen.getByTestId("home-btw-period")).not.toBeNull());
    expect(screen.getByTestId("home-btw-period").textContent).toBe("Q3 2026");
  });

  it("falls back to the dates when the period is not a whole quarter", async () => {
    home({ ...base, vat_period_start: "2026-01-01", vat_period_end: "2026-09-12" });
    await waitFor(() => expect(screen.getByTestId("home-btw-period")).not.toBeNull());
    expect(screen.getByTestId("home-btw-period").textContent).toContain("2026");
    expect(screen.getByTestId("home-btw-period").textContent).not.toMatch(/^Q\d/);
  });

  it("lists the newest invoices first, at most four", async () => {
    home(base, {
      invoices: [
        invoice("a", "2026-06-01", "2026-001"),
        invoice("b", "2026-09-18", "2026-043"),
        invoice("c", "2026-08-01", "2026-020"),
        invoice("d", "2026-07-01", "2026-010"),
        invoice("e", "2026-05-01", "2026-000"),
      ],
    });
    await waitFor(() => expect(screen.getByTestId("home-recent")).not.toBeNull());
    const rows = screen.getByTestId("home-recent").querySelectorAll("li");
    expect(rows).toHaveLength(4);
    expect(rows[0]?.textContent).toContain("2026-043");
    expect(rows[2]?.textContent).toContain("2026-010");
    expect(rows[3]?.textContent).toContain("2026-001");
    expect(screen.getByTestId("home-recent").textContent).not.toContain("2026-000");
  });

  it("leaves the recent-invoices card out, rather than erroring, when the list fails", async () => {
    home(base, { invoicesFail: true });
    await waitFor(() => expect(screen.getByTestId("home-figures")).not.toBeNull());
    expect(screen.queryByTestId("home-recent")).toBeNull();
    expect(screen.queryByTestId("home-error")).toBeNull();
  });

  it("shows a draft receipt's amount in the mono figure style and no amount for an invoice", async () => {
    home({
      ...base,
      receipts_to_review: 1,
      items_needing_action: [
        { kind: "draft_expense", id: "e1", description: "Receipt", amount: "52.80" },
        { kind: "draft_invoice", id: "i1", description: "Draft invoice", amount: null },
      ],
    });
    await waitFor(() => expect(screen.getByTestId("home-items-list")).not.toBeNull());
    const [receipt, invoice] = screen.getAllByTestId("home-item");
    expect(receipt?.querySelector(".ui-amount")?.textContent?.replace("\u00a0", " ")).toBe(
      "€ 52,80",
    );
    expect(invoice?.querySelector(".ui-amount")).toBeNull();
    expect(screen.getByText("1 receipt still needs review.")).not.toBeNull();
  });

  it("does not invent a change figure, a chart or a filing deadline", async () => {
    home(base);
    await waitFor(() => expect(screen.getByTestId("home-figures")).not.toBeNull());
    expect(document.querySelector(".ui-badge--delta")).toBeNull();
    expect(screen.queryByTestId("cash-chart")).toBeNull();
    expect(screen.queryByText(/days/i)).toBeNull();
  });

  it("draws the cash chart and the change on last month when the API supplies a history", async () => {
    home({
      ...base,
      cash_change: "3120.00",
      cash_history: [
        { month: "2026-07", balance: "18000.00" },
        { month: "2026-08", balance: "21190.45" },
        { month: "2026-09", balance: "24310.45" },
      ],
    });
    await waitFor(() => expect(screen.getByTestId("cash-chart")).not.toBeNull());
    const chip = document.querySelector(".ui-badge--delta");
    expect(chip?.textContent?.replace("\u00a0", " ")).toBe("↑ € 3.120,00");
    expect(screen.getByText(/vs August/)).not.toBeNull();
  });

  it("marks a fall with a down arrow and the amount without its sign", async () => {
    home({ ...base, cash_change: "-500.00" });
    await waitFor(() => expect(screen.getByTestId("home-figures")).not.toBeNull());
    expect(document.querySelector(".ui-badge--delta")?.textContent).toContain("↓");
    expect(document.querySelector(".ui-badge--delta")?.textContent).not.toContain("-");
  });

  it("draws no chart from a single point, which would be a dot", async () => {
    home({ ...base, cash_history: [{ month: "2026-09", balance: "24310.45" }] });
    await waitFor(() => expect(screen.getByTestId("home-figures")).not.toBeNull());
    expect(screen.queryByTestId("cash-chart")).toBeNull();
  });

  it("shows the next return's due date and the days left from the server's rule", async () => {
    const now = new Date();
    const inTen = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 10);
    const iso = `${inTen.getFullYear()}-${String(inTen.getMonth() + 1).padStart(2, "0")}-${String(inTen.getDate()).padStart(2, "0")}`;
    home({
      ...base,
      vat_return: {
        period_start: "2026-07-01",
        period_end: "2026-09-30",
        due_date: iso,
        estimate: "999.99",
      },
    });
    await waitFor(() => expect(screen.getByTestId("home-btw-due")).not.toBeNull());
    expect(screen.getByTestId("home-btw-days").textContent).toBe("10 days left");
    expect(screen.getByTestId("home-btw-period").textContent).toContain("Q3 2026");
    expect(document.body.textContent).toContain("999,99");
  });

  it("shows no due date or days when the API sends no return", async () => {
    home(base);
    await waitFor(() => expect(screen.getByTestId("home-btw-period")).not.toBeNull());
    expect(screen.queryByTestId("home-btw-due")).toBeNull();
    expect(screen.queryByTestId("home-btw-days")).toBeNull();
  });
});
