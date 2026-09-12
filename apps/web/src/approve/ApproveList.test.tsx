import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { ExpenseSummaryView, ExpenseView } from "@ledgr/shared-types";

import type { CaptureApi } from "../capture/api";
import { ApproveList } from "./ApproveList";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

const draftSummary: ExpenseSummaryView = {
  id: "exp-1",
  status: "draft",
  capture_item_id: "item-1",
  expense_date: "2026-09-01",
  supplier: "Café Central",
  gross_amount: "12.50",
  category: null,
  missing_fields: ["gross_amount"],
  can_be_marked_ready: false,
};

const untitledSummary: ExpenseSummaryView = {
  ...draftSummary,
  id: "exp-2",
  supplier: null,
  expense_date: null,
  gross_amount: null,
};

const fullView: ExpenseView = {
  id: "exp-1",
  status: "draft",
  capture_item_id: "item-1",
  expense_date: "2026-09-01",
  supplier: "Café Central",
  gross_amount: "12.50",
  vat_treatment: "btw_9",
  vat_rate: "9.00",
  vat_amount: "1.03",
  net_amount: "11.47",
  category: "Representatie",
  payment_method: "business_card",
  suggested_category: null,
  missing_fields: [],
  can_be_marked_ready: true,
  duplicate_warnings: [],
};

function api(overrides: Partial<CaptureApi> = {}): CaptureApi {
  return {
    listExpenses: vi.fn(async () => [draftSummary]),
    getExpense: vi.fn(async () => fullView),
    updateExpense: vi.fn(async () => fullView),
    markReady: vi.fn(async () => ({ ...fullView, status: "ready" as const })),
    ...overrides,
  } as unknown as CaptureApi;
}

describe("ApproveList — MOB-004's revisitable draft review", () => {
  it("shows the administration's draft expenses", async () => {
    const list = api();
    render(<ApproveList administrationId="adm-A" api={list} />);

    await waitFor(() => expect(screen.getByTestId("approve-list")).toBeDefined());
    expect(screen.getByTestId("approve-item-supplier").textContent).toBe("Café Central");
    expect(list.listExpenses).toHaveBeenCalledWith("adm-A", "draft");
  });

  it("shows an untitled placeholder for a barely-started capture", async () => {
    render(
      <ApproveList
        administrationId="adm-A"
        api={api({ listExpenses: vi.fn(async () => [untitledSummary]) })}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("approve-item-supplier")).toBeDefined());
    expect(screen.getByTestId("approve-item-supplier").textContent).not.toBe("");
  });

  it("shows an empty state when there is nothing to review", async () => {
    render(
      <ApproveList administrationId="adm-A" api={api({ listExpenses: vi.fn(async () => []) })} />,
    );

    await waitFor(() => expect(screen.getByTestId("approve-empty")).toBeDefined());
  });

  it("tapping an item reaches the existing ExpenseForm", async () => {
    render(<ApproveList administrationId="adm-A" api={api()} />);

    await waitFor(() => expect(screen.getByTestId("approve-open-exp-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("approve-open-exp-1"));

    await waitFor(() => expect(screen.getByTestId("expense-save")).toBeDefined());
    expect(screen.getByTestId("expense-supplier")).toHaveProperty("value", "Café Central");
  });

  it("returns to the list once the expense is marked ready, and it no longer appears", async () => {
    const list = api({
      listExpenses: vi.fn().mockResolvedValueOnce([draftSummary]).mockResolvedValueOnce([]),
    });
    render(<ApproveList administrationId="adm-A" api={list} />);

    await waitFor(() => expect(screen.getByTestId("approve-open-exp-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("approve-open-exp-1"));

    await waitFor(() => expect(screen.getByTestId("expense-submit")).toBeDefined());
    fireEvent.click(screen.getByTestId("expense-submit"));

    await waitFor(() => expect(screen.getByTestId("approve-empty")).toBeDefined());
    expect(list.listExpenses).toHaveBeenCalledTimes(2);
  });
});
